from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import patch

import httpx
from celery.exceptions import Retry
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Organization, OrganizationMembership
from apps.billing.models import BillingPlanConfiguration, OrganizationSubscription, StripeWebhookEvent
from apps.billing.views import sync_subscription_object
from apps.documents.models import AdminAuditLog
from services.stripe_client import StripeAPIClient
from tasks.notifications import (
    SEAT_SYNC_MAX_DELAY_SECONDS,
    SEAT_SYNC_MAX_RETRIES,
    seat_sync_retry_delay,
    seat_sync_should_retry,
    sync_organization_seat_quantity,
)


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    STRIPE_SECRET_KEY="sk_test_secret",
    STRIPE_WEBHOOK_SECRET="whsec_test_secret",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class BillingApiTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.owner = get_user_model().objects.create_user(
            username="owner",
            email="owner@example.com",
            password="password123",
            is_staff=True,
        )
        self.organization = Organization.objects.create(name="Example Company", created_by=self.owner)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        self.professional_plan, _ = BillingPlanConfiguration.objects.update_or_create(
            plan=BillingPlanConfiguration.PlanEnum.PROFESSIONAL,
            billing_interval=BillingPlanConfiguration.BillingIntervalEnum.MONTH,
            defaults={
                "amount": Decimal("9.99"),
                "currency": BillingPlanConfiguration.CurrencyEnum.USD,
                "is_active": True,
            },
        )
        self.authenticate(self.owner)

    def authenticate(self, user) -> None:
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def test_billing_status_is_scoped_to_current_company(self) -> None:
        response = self.client.get("/api/admin/billing/")

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["organization"], str(self.organization.id))
        self.assertEqual(response.json()["plan"], OrganizationSubscription.PlanEnum.FREE)
        professional = next(
            plan
            for plan in response.json()["available_plans"]
            if plan["plan"] == BillingPlanConfiguration.PlanEnum.PROFESSIONAL
        )
        self.assertEqual(professional["amount"], "9.99")
        self.assertTrue(
            AdminAuditLog.objects.filter(
                organization=self.organization,
                action=AdminAuditLog.ActionEnum.BILLING_VIEW,
            ).exists()
        )

    def test_active_plan_prices_are_public_without_service_credentials(self) -> None:
        BillingPlanConfiguration.objects.update_or_create(
            plan=BillingPlanConfiguration.PlanEnum.BUSINESS,
            billing_interval=BillingPlanConfiguration.BillingIntervalEnum.MONTH,
            defaults={
                "amount": Decimal("19.99"),
                "currency": BillingPlanConfiguration.CurrencyEnum.USD,
                "is_active": False,
            },
        )
        self.client.credentials()

        response = self.client.get("/api/billing/plans/")

        self.assertEqual(response.status_code, 200, response.json())
        professional_plans = [
            plan for plan in response.json() if plan["plan"] == BillingPlanConfiguration.PlanEnum.PROFESSIONAL
        ]
        self.assertTrue(professional_plans)
        self.assertIn("9.99", {plan["amount"] for plan in professional_plans})

    @patch("apps.billing.views.StripeAPIClient")
    def test_owner_can_create_checkout_session(self, stripe_client_class) -> None:
        stripe_client = stripe_client_class.return_value
        stripe_client.create_customer.return_value = {"id": "cus_123"}
        stripe_client.create_checkout_session.return_value = {"url": "https://checkout.stripe.com/test"}

        response = self.client.post(
            "/api/admin/billing/checkout/",
            {
                "plan": OrganizationSubscription.PlanEnum.PROFESSIONAL,
                "billing_interval": BillingPlanConfiguration.BillingIntervalEnum.MONTH,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["checkout_url"], "https://checkout.stripe.com/test")
        subscription = OrganizationSubscription.objects.get(organization=self.organization)
        self.assertEqual(subscription.stripe_customer_id, "cus_123")
        self.assertEqual(subscription.plan, OrganizationSubscription.PlanEnum.PROFESSIONAL)
        stripe_client.create_checkout_session.assert_called_once_with(
            customer_id="cus_123",
            organization_id=str(self.organization.id),
            plan=OrganizationSubscription.PlanEnum.PROFESSIONAL,
            plan_name="Professional",
            amount=Decimal("9.99"),
            currency="USD",
            billing_interval="month",
            quantity=1,
        )

    @patch("apps.billing.views.StripeAPIClient")
    def test_billing_status_reconciles_cancellation_from_stripe(self, stripe_client_class) -> None:
        subscription = OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=OrganizationSubscription.PlanEnum.PROFESSIONAL,
            status=OrganizationSubscription.StatusEnum.ACTIVE,
            stripe_customer_id="cus_123",
            stripe_subscription_id="sub_123",
        )
        stripe_client_class.return_value.retrieve_subscription.return_value = {
            "id": "sub_123",
            "customer": "cus_123",
            "status": "canceled",
            "cancel_at_period_end": False,
            "metadata": {
                "organization_id": str(self.organization.id),
                "plan": "PROFESSIONAL",
            },
        }

        response = self.client.get("/api/admin/billing/")

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["status"], OrganizationSubscription.StatusEnum.CANCELED)
        self.assertFalse(response.json()["cancel_at_period_end"])
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, OrganizationSubscription.StatusEnum.CANCELED)

    @patch("apps.billing.views.enqueue_task")
    @patch("apps.billing.views.StripeAPIClient")
    def test_checkout_status_confirms_payment_and_activates_plan(self, stripe_client_class, enqueue_task) -> None:
        stripe_client = stripe_client_class.return_value
        stripe_client.retrieve_checkout_session.return_value = {
            "id": "cs_test_123",
            "status": "complete",
            "payment_status": "paid",
            "customer": "cus_123",
            "subscription": "sub_123",
            "metadata": {
                "organization_id": str(self.organization.id),
                "plan": "PROFESSIONAL",
            },
        }
        stripe_client.retrieve_subscription.return_value = {
            "id": "sub_123",
            "customer": "cus_123",
            "status": "active",
            "current_period_end": 1_800_000_000,
            "cancel_at_period_end": False,
            "metadata": {
                "organization_id": str(self.organization.id),
                "plan": "PROFESSIONAL",
            },
            "items": {"data": [{"price": {"id": "price_professional"}}]},
        }

        response = self.client.post(
            "/api/admin/billing/checkout-status/",
            {"session_id": "cs_test_123"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["status"], OrganizationSubscription.StatusEnum.ACTIVE)
        self.assertEqual(response.json()["plan"], OrganizationSubscription.PlanEnum.PROFESSIONAL)
        stripe_client.retrieve_checkout_session.assert_called_once_with("cs_test_123")
        enqueue_task.assert_called_once()

    @patch("apps.billing.views.StripeAPIClient")
    def test_checkout_status_rejects_session_from_another_workspace(self, stripe_client_class) -> None:
        stripe_client = stripe_client_class.return_value
        stripe_client.retrieve_checkout_session.return_value = {
            "id": "cs_test_other",
            "status": "complete",
            "payment_status": "paid",
            "customer": "cus_other",
            "subscription": "sub_other",
            "metadata": {
                "organization_id": "another-workspace",
                "plan": "PROFESSIONAL",
            },
        }

        response = self.client.post(
            "/api/admin/billing/checkout-status/",
            {"session_id": "cs_test_other"},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.json())
        stripe_client.retrieve_subscription.assert_not_called()

    @patch("apps.billing.views.enqueue_task")
    @patch("apps.billing.views.StripeAPIClient")
    def test_checkout_webhook_activates_plan_and_queues_confirmation_email(
        self, stripe_client_class, enqueue_task
    ) -> None:
        stripe_client_class.return_value.retrieve_subscription.return_value = {
            "id": "sub_checkout",
            "customer": "cus_checkout",
            "status": "active",
            "current_period_end": 1_800_000_000,
            "cancel_at_period_end": False,
            "metadata": {
                "organization_id": str(self.organization.id),
                "plan": "PROFESSIONAL",
            },
            "items": {"data": [{"price": {"id": "price_professional"}}]},
        }
        payload = {
            "id": "evt_checkout_completed",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_checkout",
                    "status": "complete",
                    "payment_status": "paid",
                    "customer": "cus_checkout",
                    "subscription": "sub_checkout",
                    "metadata": {
                        "organization_id": str(self.organization.id),
                        "plan": "PROFESSIONAL",
                    },
                }
            },
        }
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
        self.client.credentials()

        response = self.client.post(
            "/api/billing/webhooks/stripe/",
            data=raw_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=self.sign_payload(raw_body),
        )

        self.assertEqual(response.status_code, 200, response.json())
        subscription = OrganizationSubscription.objects.get(organization=self.organization)
        self.assertEqual(subscription.status, OrganizationSubscription.StatusEnum.ACTIVE)
        enqueue_task.assert_called_once()

    def test_inactive_plan_cannot_start_checkout(self) -> None:
        self.professional_plan.is_active = False
        self.professional_plan.save(update_fields=["is_active", "updated_at"])

        response = self.client.post(
            "/api/admin/billing/checkout/",
            {
                "plan": OrganizationSubscription.PlanEnum.PROFESSIONAL,
                "billing_interval": BillingPlanConfiguration.BillingIntervalEnum.MONTH,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 503, response.json())

    def test_active_subscription_must_use_billing_portal_for_plan_changes(self) -> None:
        OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=OrganizationSubscription.PlanEnum.PROFESSIONAL,
            status=OrganizationSubscription.StatusEnum.ACTIVE,
            stripe_customer_id="cus_123",
            stripe_subscription_id="sub_123",
        )

        response = self.client.post(
            "/api/admin/billing/checkout/",
            {
                "plan": OrganizationSubscription.PlanEnum.BUSINESS,
                "billing_interval": BillingPlanConfiguration.BillingIntervalEnum.MONTH,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()["detail"], "Use the billing portal to change an active subscription.")

    def test_non_owner_cannot_manage_billing(self) -> None:
        admin_user = get_user_model().objects.create_user(
            username="company-admin",
            password="password123",
            is_staff=True,
        )
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=admin_user,
            role=OrganizationMembership.RoleEnum.ADMIN,
        )
        self.authenticate(admin_user)

        response = self.client.post(
            "/api/admin/billing/checkout/",
            {
                "plan": OrganizationSubscription.PlanEnum.BUSINESS,
                "billing_interval": BillingPlanConfiguration.BillingIntervalEnum.MONTH,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.json())

    def test_signed_webhook_updates_subscription_and_rejects_replay_mismatch(self) -> None:
        payload = {
            "id": "evt_subscription_updated",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_123",
                    "customer": "cus_123",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_end": 1_800_000_000,
                    "metadata": {
                        "organization_id": str(self.organization.id),
                        "plan": "PROFESSIONAL",
                    },
                    "items": {"data": [{"price": {"id": "price_professional"}}]},
                }
            },
        }
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
        signature = self.sign_payload(raw_body)
        self.client.credentials()

        response = self.client.post(
            "/api/billing/webhooks/stripe/",
            data=raw_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 200, response.json())
        subscription = OrganizationSubscription.objects.get(organization=self.organization)
        self.assertEqual(subscription.status, OrganizationSubscription.StatusEnum.ACTIVE)
        self.assertEqual(subscription.stripe_subscription_id, "sub_123")
        event = StripeWebhookEvent.objects.get(stripe_event_id=payload["id"])
        self.assertEqual(event.status, StripeWebhookEvent.ProcessingStatusEnum.PROCESSED)

        changed_body = raw_body.replace(b"price_professional", b"price_business")
        changed_response = self.client.post(
            "/api/billing/webhooks/stripe/",
            data=changed_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=self.sign_payload(changed_body),
        )
        self.assertEqual(changed_response.status_code, 400, changed_response.json())

    @patch("apps.billing.views.StripeAPIClient")
    def test_billing_status_preserves_scheduled_cancellation(self, stripe_client_class) -> None:
        OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=OrganizationSubscription.PlanEnum.PROFESSIONAL,
            status=OrganizationSubscription.StatusEnum.ACTIVE,
            stripe_customer_id="cus_123",
            stripe_subscription_id="sub_123",
        )
        stripe_client_class.return_value.retrieve_subscription.return_value = {
            "id": "sub_123",
            "customer": "cus_123",
            "status": "active",
            "cancel_at_period_end": True,
            "current_period_end": 1_800_000_000,
            "metadata": {
                "organization_id": str(self.organization.id),
                "plan": "PROFESSIONAL",
            },
        }

        response = self.client.get("/api/admin/billing/")

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["status"], OrganizationSubscription.StatusEnum.ACTIVE)
        self.assertTrue(response.json()["cancel_at_period_end"])

    def test_webhook_rejects_invalid_signature(self) -> None:
        self.client.credentials()
        response = self.client.post(
            "/api/billing/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=invalid",
        )
        self.assertEqual(response.status_code, 400, response.json())

    @staticmethod
    def sign_payload(payload: bytes) -> str:
        timestamp = int(time.time())
        signed_payload = str(timestamp).encode() + b"." + payload
        digest = hmac.new(b"whsec_test_secret", signed_payload, hashlib.sha256).hexdigest()
        return f"t={timestamp},v1={digest}"


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    STRIPE_SECRET_KEY="sk_test_secret",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class SeatQuantitySyncTests(TestCase):
    """Billing a Business workspace for the people actually in it.

    Seats are set once, at checkout. Everything after that - somebody accepting an invitation,
    somebody being removed - depends on this task, and a workspace that is charged per member
    has to be charged for the members it has.
    """

    def setUp(self) -> None:
        self.owner = get_user_model().objects.create_user(
            username="seat-owner",
            email="seat-owner@example.com",
            password="password123",
        )
        self.organization = Organization.objects.create(name="Seat Company", created_by=self.owner)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        self.subscription = OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=OrganizationSubscription.PlanEnum.BUSINESS,
            status=OrganizationSubscription.StatusEnum.ACTIVE,
            stripe_customer_id="cus_seat",
            stripe_subscription_id="sub_seat",
            stripe_subscription_item_id="si_seat",
            stripe_subscription_quantity=1,
        )

    def add_member(self, suffix: str) -> OrganizationMembership:
        user = get_user_model().objects.create_user(
            username=f"member-{suffix}",
            email=f"member-{suffix}@example.com",
            password="password123",
        )
        return OrganizationMembership.objects.create(
            organization=self.organization,
            user=user,
            role=OrganizationMembership.RoleEnum.MEMBER,
        )

    @patch("services.stripe_client.StripeAPIClient")
    def test_growing_the_team_bills_for_the_new_seats(self, stripe_client_class) -> None:
        self.add_member("a")
        self.add_member("b")
        stripe_client_class.return_value.update_subscription_item_quantity.return_value = {"quantity": 3}

        sync_organization_seat_quantity(str(self.organization.id))

        stripe_client_class.return_value.update_subscription_item_quantity.assert_called_once_with("si_seat", 3)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.stripe_subscription_quantity, 3)

    @patch("services.stripe_client.StripeAPIClient")
    def test_the_update_names_the_subscription_item(self, stripe_client_class) -> None:
        """The bug this exists to stop coming back.

        Sending a quantity to the subscription without naming the item does not change the seats:
        Stripe reads an item with no id as a new line to add. The call has to address the item.
        """
        self.add_member("a")
        stripe_client_class.return_value.update_subscription_item_quantity.return_value = {"quantity": 2}

        sync_organization_seat_quantity(str(self.organization.id))

        client = stripe_client_class.return_value
        self.assertFalse(client.update_subscription_quantity.called)
        item_id, quantity = client.update_subscription_item_quantity.call_args.args
        self.assertEqual(item_id, "si_seat")
        self.assertEqual(quantity, 2)

    @patch("services.stripe_client.StripeAPIClient")
    def test_removing_a_member_releases_the_seat(self, stripe_client_class) -> None:
        membership = self.add_member("a")
        self.subscription.stripe_subscription_quantity = 2
        self.subscription.save(update_fields=["stripe_subscription_quantity"])
        membership.status = OrganizationMembership.StatusEnum.SUSPENDED
        membership.save(update_fields=["status"])
        stripe_client_class.return_value.update_subscription_item_quantity.return_value = {"quantity": 1}

        sync_organization_seat_quantity(str(self.organization.id))

        stripe_client_class.return_value.update_subscription_item_quantity.assert_called_once_with("si_seat", 1)

    @patch("services.stripe_client.StripeAPIClient")
    def test_a_deactivated_user_does_not_hold_a_seat(self, stripe_client_class) -> None:
        membership = self.add_member("a")
        membership.user.is_active = False
        membership.user.save(update_fields=["is_active"])
        stripe_client_class.return_value.update_subscription_item_quantity.return_value = {"quantity": 1}

        sync_organization_seat_quantity(str(self.organization.id))

        self.assertFalse(stripe_client_class.return_value.update_subscription_item_quantity.called)

    @patch("services.stripe_client.StripeAPIClient")
    def test_no_change_means_no_call(self, stripe_client_class) -> None:
        sync_organization_seat_quantity(str(self.organization.id))

        self.assertFalse(stripe_client_class.return_value.update_subscription_item_quantity.called)

    @patch("services.stripe_client.StripeAPIClient")
    def test_other_plans_stay_at_one_seat(self, stripe_client_class) -> None:
        self.add_member("a")
        self.add_member("b")
        self.subscription.plan = OrganizationSubscription.PlanEnum.PROFESSIONAL
        self.subscription.stripe_subscription_quantity = 4
        self.subscription.save(update_fields=["plan", "stripe_subscription_quantity"])
        stripe_client_class.return_value.update_subscription_item_quantity.return_value = {"quantity": 1}

        sync_organization_seat_quantity(str(self.organization.id))

        stripe_client_class.return_value.update_subscription_item_quantity.assert_called_once_with("si_seat", 1)

    @patch("services.stripe_client.StripeAPIClient")
    def test_an_older_subscription_learns_its_item_id_once(self, stripe_client_class) -> None:
        """Subscriptions opened before the id was stored still have to be billable."""
        self.subscription.stripe_subscription_item_id = ""
        self.subscription.stripe_subscription_quantity = None
        self.subscription.save(update_fields=["stripe_subscription_item_id", "stripe_subscription_quantity"])
        self.add_member("a")
        client = stripe_client_class.return_value
        client.retrieve_subscription.return_value = {
            "id": "sub_seat",
            "items": {"data": [{"id": "si_recovered", "quantity": 1}]},
        }
        client.update_subscription_item_quantity.return_value = {"quantity": 2}

        sync_organization_seat_quantity(str(self.organization.id))

        client.update_subscription_item_quantity.assert_called_once_with("si_recovered", 2)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.stripe_subscription_item_id, "si_recovered")
        self.assertEqual(self.subscription.stripe_subscription_quantity, 2)

    @patch("services.stripe_client.StripeAPIClient")
    def test_a_workspace_without_a_subscription_is_left_alone(self, stripe_client_class) -> None:
        self.subscription.stripe_subscription_id = ""
        self.subscription.save(update_fields=["stripe_subscription_id"])

        sync_organization_seat_quantity(str(self.organization.id))

        self.assertFalse(stripe_client_class.return_value.update_subscription_item_quantity.called)

    def test_the_webhook_records_the_item_the_seats_are_billed_on(self) -> None:
        """The id is in every subscription webhook, so it should never need fetching twice."""
        self.subscription.stripe_subscription_item_id = ""
        self.subscription.save(update_fields=["stripe_subscription_item_id"])

        sync_subscription_object(
            {
                "id": "sub_seat",
                "customer": "cus_seat",
                "status": "active",
                "cancel_at_period_end": False,
                "metadata": {"organization_id": str(self.organization.id), "plan": "BUSINESS"},
                "items": {"data": [{"id": "si_from_webhook", "quantity": 5, "price": {"id": "price_seat"}}]},
            },
            notify=False,
        )

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.stripe_subscription_item_id, "si_from_webhook")
        self.assertEqual(self.subscription.stripe_subscription_quantity, 5)


@override_settings(STRIPE_SECRET_KEY="sk_test_secret")
class StripeSeatRequestTests(TestCase):
    """What actually goes over the wire when seats change.

    The original defect was not in which method got called, it was in the body of the request:
    a quantity posted to the subscription with no item id. Stripe reads that as a new line to
    add rather than a change to the existing one, so the seat count never moved. Asserting on
    the request is the only way that failure is visible from a test.
    """

    @patch("services.stripe_client.BaseAPIClient.post")
    def test_the_quantity_is_addressed_to_the_item(self, post) -> None:
        post.return_value.json.return_value = {"id": "si_123", "quantity": 4}

        StripeAPIClient().update_subscription_item_quantity("si_123", 4)

        path = post.call_args.args[0]
        data = post.call_args.kwargs["data"]
        self.assertEqual(path, "/subscription_items/si_123")
        self.assertEqual(data["quantity"], "4")
        self.assertEqual(data["proration_behavior"], "create_prorations")
        # Nothing addressed at the subscription, and no bare items[] that Stripe would read as
        # a line to add.
        self.assertNotIn("/subscriptions/", path)
        self.assertFalse([key for key in data if key.startswith("items[")])

    @patch("services.stripe_client.BaseAPIClient.post")
    def test_a_quantity_below_one_is_not_sent(self, post) -> None:
        """Stripe rejects a zero quantity; an empty workspace still owns its subscription."""
        post.return_value.json.return_value = {"id": "si_123", "quantity": 1}

        StripeAPIClient().update_subscription_item_quantity("si_123", 0)

        self.assertEqual(post.call_args.kwargs["data"]["quantity"], "1")


class SeatSyncRetryTests(TestCase):
    """Seats are the one piece of work here that moves money.

    An email that fails to send is noticed by the person waiting for it. A seat count that fails
    to reach Stripe is noticed by nobody, and the workspace is quietly billed for the wrong
    number of people until somebody changes the team again.
    """

    def status_error(self, code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://api.stripe.com/v1/subscription_items/si_1")
        return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))

    def test_a_bad_minute_at_stripe_is_worth_trying_again(self) -> None:
        for code in (429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertTrue(seat_sync_should_retry(self.status_error(code)))

    def test_a_request_stripe_rejected_is_not(self) -> None:
        """A 400 means the request was wrong and will be wrong again. Retrying only hides it."""
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                self.assertFalse(seat_sync_should_retry(self.status_error(code)))

    def test_a_request_that_never_got_an_answer_is_worth_trying_again(self) -> None:
        request = httpx.Request("POST", "https://api.stripe.com/v1/subscription_items/si_1")
        self.assertTrue(seat_sync_should_retry(httpx.ConnectTimeout("timed out", request=request)))
        self.assertTrue(seat_sync_should_retry(httpx.ConnectError("refused", request=request)))

    def test_the_wait_grows_and_then_stops_growing(self) -> None:
        """An outage ends for everybody at once; a fixed delay makes the recovery a stampede."""
        delays = [seat_sync_retry_delay(attempt) for attempt in range(SEAT_SYNC_MAX_RETRIES + 1)]

        self.assertEqual(delays[0], 30)
        self.assertEqual(delays, sorted(delays))
        self.assertLessEqual(max(delays), SEAT_SYNC_MAX_DELAY_SECONDS)
        # Five attempts have to fit inside something a person would wait for.
        self.assertLess(sum(delays[:SEAT_SYNC_MAX_RETRIES]), 3600)


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    STRIPE_SECRET_KEY="sk_test_secret",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class SeatSyncRetryBehaviourTests(TestCase):
    """What the task actually does when Stripe does not answer."""

    def setUp(self) -> None:
        self.owner = get_user_model().objects.create_user(
            username="retry-owner", email="retry@example.com", password="password123"
        )
        self.organization = Organization.objects.create(name="Retry Company", created_by=self.owner)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        OrganizationSubscription.objects.create(
            organization=self.organization,
            plan=OrganizationSubscription.PlanEnum.BUSINESS,
            status=OrganizationSubscription.StatusEnum.ACTIVE,
            stripe_customer_id="cus_retry",
            stripe_subscription_id="sub_retry",
            stripe_subscription_item_id="si_retry",
            stripe_subscription_quantity=9,
        )

    def failing_client(self, error: Exception):
        client = patch("services.stripe_client.StripeAPIClient").start()
        self.addCleanup(patch.stopall)
        client.return_value.update_subscription_item_quantity.side_effect = error
        return client

    def timeout(self) -> httpx.ConnectTimeout:
        request = httpx.Request("POST", "https://api.stripe.com/v1/subscription_items/si_retry")
        return httpx.ConnectTimeout("timed out", request=request)

    def rejected(self) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://api.stripe.com/v1/subscription_items/si_retry")
        return httpx.HTTPStatusError("bad request", request=request, response=httpx.Response(400, request=request))

    def test_a_timeout_is_scheduled_to_be_tried_again(self) -> None:
        self.failing_client(self.timeout())

        # Celery re-raises rather than scheduling when a task is called outside a worker, so the
        # decision is what can be observed here. The scheduling itself is Celery's to do.
        with patch.object(sync_organization_seat_quantity, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                sync_organization_seat_quantity(str(self.organization.id))

        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.kwargs["countdown"], 30)
        self.assertEqual(retry.call_args.kwargs["max_retries"], SEAT_SYNC_MAX_RETRIES)

    def test_a_rejected_request_is_raised_rather_than_repeated(self) -> None:
        """Retrying a 400 five times hides it for half an hour and changes nothing."""
        self.failing_client(self.rejected())

        with patch.object(sync_organization_seat_quantity, "retry") as retry:
            with self.assertRaises(httpx.HTTPStatusError):
                sync_organization_seat_quantity(str(self.organization.id))

        self.assertFalse(retry.called)

    def test_the_quantity_is_not_recorded_when_the_call_failed(self) -> None:
        """Otherwise a retry would see a number that matches and decide there is nothing to do."""
        self.failing_client(self.timeout())

        with patch.object(sync_organization_seat_quantity, "retry", side_effect=Retry()):
            with self.assertRaises(Retry):
                sync_organization_seat_quantity(str(self.organization.id))

        subscription = OrganizationSubscription.objects.get(organization=self.organization)
        self.assertEqual(subscription.stripe_subscription_quantity, 9)
