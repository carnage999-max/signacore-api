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
        self.professional_plan = BillingPlanConfiguration.objects.create(
            plan=BillingPlanConfiguration.PlanEnum.PROFESSIONAL,
            amount=Decimal("29.00"),
            currency=BillingPlanConfiguration.CurrencyEnum.USD,
            billing_interval=BillingPlanConfiguration.BillingIntervalEnum.MONTH,
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
        self.assertEqual(response.json()["available_plans"][0]["amount"], "29.00")
        self.assertTrue(
            AdminAuditLog.objects.filter(
                organization=self.organization,
                action=AdminAuditLog.ActionEnum.BILLING_VIEW,
            ).exists()
        )

    @patch("apps.billing.views.StripeAPIClient")
    def test_owner_can_create_checkout_session(self, stripe_client_class) -> None:
        stripe_client = stripe_client_class.return_value
        stripe_client.create_customer.return_value = {"id": "cus_123"}
        stripe_client.create_checkout_session.return_value = {"url": "https://checkout.stripe.com/test"}

        response = self.client.post(
            "/api/admin/billing/checkout/",
            {"plan": OrganizationSubscription.PlanEnum.PROFESSIONAL},
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
            amount=Decimal("29.00"),
            currency="USD",
            billing_interval="month",
        )

    def test_inactive_plan_cannot_start_checkout(self) -> None:
        self.professional_plan.is_active = False
        self.professional_plan.save(update_fields=["is_active", "updated_at"])

        response = self.client.post(
            "/api/admin/billing/checkout/",
            {"plan": OrganizationSubscription.PlanEnum.PROFESSIONAL},
            format="json",
        )

        self.assertEqual(response.status_code, 503, response.json())

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
            {"plan": OrganizationSubscription.PlanEnum.BUSINESS},
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
