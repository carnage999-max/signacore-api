from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Organization, OrganizationMembership
from apps.billing.models import BillingPlanConfiguration, OrganizationSubscription, StripeWebhookEvent
from apps.documents.models import AdminAuditLog


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
