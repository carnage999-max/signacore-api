from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any

from django.conf import settings

from utils.base_api_client import BaseAPIClient


class StripeAPIClient(BaseAPIClient):
    def __init__(self) -> None:
        super().__init__(
            "https://api.stripe.com/v1",
            headers={
                "Authorization": f"Bearer {settings.STRIPE_SECRET_KEY}",
                "Stripe-Version": settings.STRIPE_API_VERSION,
            },
        )

    def create_customer(self, *, organization_id: str, organization_name: str) -> dict[str, Any]:
        response = self.post(
            "/customers",
            headers={"Idempotency-Key": f"signacore-customer-{organization_id}"},
            data={
                "name": organization_name,
                "metadata[organization_id]": organization_id,
            },
        )
        return response.json()

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        organization_id: str,
        plan: str,
        plan_name: str,
        amount: Decimal,
        currency: str,
        billing_interval: str,
    ) -> dict[str, Any]:
        app_url = settings.SIGNACORE_APP_URL.rstrip("/")
        response = self.post(
            "/checkout/sessions",
            data={
                "mode": "subscription",
                "customer": customer_id,
                "line_items[0][price_data][currency]": currency.lower(),
                "line_items[0][price_data][unit_amount]": str(int(amount * Decimal("100"))),
                "line_items[0][price_data][recurring][interval]": billing_interval,
                "line_items[0][price_data][product_data][name]": f"SignaCore {plan_name}",
                "line_items[0][quantity]": "1",
                "success_url": f"{app_url}/admin/billing?checkout=success",
                "cancel_url": f"{app_url}/admin/billing?checkout=cancelled",
                "metadata[organization_id]": organization_id,
                "metadata[plan]": plan,
                "subscription_data[metadata][organization_id]": organization_id,
                "subscription_data[metadata][plan]": plan,
            },
        )
        return response.json()

    def create_portal_session(self, *, customer_id: str) -> dict[str, Any]:
        response = self.post(
            "/billing_portal/sessions",
            data={
                "customer": customer_id,
                "return_url": f"{settings.SIGNACORE_APP_URL.rstrip('/')}/admin/billing",
            },
        )
        return response.json()

    def retrieve_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self.get(f"/subscriptions/{subscription_id}").json()


def verify_stripe_signature(
    payload: bytes,
    signature_header: str,
    webhook_secret: str,
    tolerance_seconds: int = 300,
) -> bool:
    values: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, separator, value = item.partition("=")
        if separator:
            values.setdefault(key.strip(), []).append(value.strip())

    try:
        timestamp = int(values.get("t", [""])[0])
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp) > tolerance_seconds:
        return False

    signed_payload = str(timestamp).encode("utf-8") + b"." + payload
    expected = hmac.new(webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, signature) for signature in values.get("v1", []))
