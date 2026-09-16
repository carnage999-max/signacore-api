from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx
from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Organization, OrganizationMembership
from apps.documents.auth import HasValidSignacoreSecret
from apps.documents.models import AdminAuditLog
from apps.documents.views import get_request_actor_and_organization, log_admin_event
from services.stripe_client import StripeAPIClient, verify_stripe_signature

from .models import BillingPlanConfiguration, OrganizationSubscription, StripeWebhookEvent
from .serializers import (
    BillingPlanConfigurationSerializer,
    BillingPortalResponseSerializer,
    CheckoutSessionResponseSerializer,
    CheckoutSessionSerializer,
    OrganizationSubscriptionSerializer,
    StripeWebhookResponseSerializer,
)


def require_billing_manager(actor, organization: Organization) -> None:
    if actor.is_superuser:
        return
    if not OrganizationMembership.objects.filter(
        user=actor,
        organization=organization,
        status=OrganizationMembership.StatusEnum.ACTIVE,
        role=OrganizationMembership.RoleEnum.OWNER,
    ).exists():
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("Company owner access is required to manage billing.")


def get_organization_subscription(organization: Organization) -> OrganizationSubscription:
    subscription, _ = OrganizationSubscription.objects.get_or_create(organization=organization)
    return subscription


def get_organization_seat_count(organization: Organization, plan: str) -> int:
    if plan != OrganizationSubscription.PlanEnum.BUSINESS:
        return 1
    return max(
        organization.memberships.filter(
            status=OrganizationMembership.StatusEnum.ACTIVE,
            user__is_active=True,
        ).count(),
        1,
    )


def timestamp_to_datetime(value) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def sync_subscription_object(payload: dict) -> bool:
    subscription_id = str(payload.get("id") or "")
    customer_id = str(payload.get("customer") or "")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    organization_id = str(metadata.get("organization_id") or "")

    organization = Organization.objects.filter(pk=organization_id).first() if organization_id else None
    existing = None
    if organization is None and subscription_id:
        existing = OrganizationSubscription.objects.filter(stripe_subscription_id=subscription_id).first()
    if organization is None and customer_id:
        existing = existing or OrganizationSubscription.objects.filter(stripe_customer_id=customer_id).first()
    if organization is None and existing:
        organization = existing.organization
    if organization is None:
        return False

    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    item_data = items.get("data") if isinstance(items.get("data"), list) else []
    first_item = item_data[0] if item_data and isinstance(item_data[0], dict) else {}
    price = first_item.get("price") if isinstance(first_item.get("price"), dict) else {}
    price_id = str(price.get("id") or "")
    period_end = payload.get("current_period_end") or first_item.get("current_period_end")
    stripe_status = str(payload.get("status") or "none").upper()
    valid_statuses = {value for value, _ in OrganizationSubscription.StatusEnum.choices}

    subscription = get_organization_subscription(organization)
    subscription.stripe_customer_id = customer_id or subscription.stripe_customer_id
    subscription.stripe_subscription_id = subscription_id or subscription.stripe_subscription_id
    subscription.stripe_price_id = price_id or subscription.stripe_price_id
    metadata_plan = str(metadata.get("plan") or "").upper()
    valid_plans = {value for value, _ in OrganizationSubscription.PlanEnum.choices}
    if metadata_plan in valid_plans:
        subscription.plan = metadata_plan
    subscription.status = stripe_status if stripe_status in valid_statuses else OrganizationSubscription.StatusEnum.NONE
    subscription.current_period_end = timestamp_to_datetime(period_end)
    subscription.cancel_at_period_end = bool(payload.get("cancel_at_period_end", False))
    subscription.save()
    return True


class PublicBillingPlanListView(APIView):
    authentication_classes = []
    permission_classes = []
    serializer_class = BillingPlanConfigurationSerializer

    @extend_schema(responses=BillingPlanConfigurationSerializer(many=True))
    def get(self, request):
        plans = BillingPlanConfiguration.objects.filter(is_active=True)
        return Response(BillingPlanConfigurationSerializer(plans, many=True).data)


class BillingStatusView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = OrganizationSubscriptionSerializer

    def get(self, request):
        actor, organization = get_request_actor_and_organization(request)
        subscription = get_organization_subscription(organization)
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.BILLING_VIEW,
            f"Viewed billing for {organization.name}.",
            target_type="organization",
            target_id=organization.id,
        )
        return Response(OrganizationSubscriptionSerializer(subscription, context={"actor": actor}).data)


class BillingCheckoutView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = CheckoutSessionSerializer

    @extend_schema(responses={201: CheckoutSessionResponseSerializer})
    def post(self, request):
        actor, organization = get_request_actor_and_organization(request)
        require_billing_manager(actor, organization)
        serializer = CheckoutSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.validated_data["plan"]
        billing_interval = serializer.validated_data["billing_interval"]
        plan_configuration = BillingPlanConfiguration.objects.filter(
            plan=plan,
            billing_interval=billing_interval,
            is_active=True,
        ).first()
        if not settings.STRIPE_SECRET_KEY or plan_configuration is None:
            return Response(
                {"detail": "This subscription plan is not available."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        subscription = get_organization_subscription(organization)
        if subscription.status in {
            OrganizationSubscription.StatusEnum.ACTIVE,
            OrganizationSubscription.StatusEnum.TRIALING,
        }:
            return Response(
                {"detail": "Use the billing portal to change an active subscription."},
                status=status.HTTP_409_CONFLICT,
            )
        client = StripeAPIClient()
        try:
            if not subscription.stripe_customer_id:
                customer = client.create_customer(
                    organization_id=str(organization.id),
                    organization_name=organization.name,
                )
                subscription.stripe_customer_id = str(customer["id"])
            checkout = client.create_checkout_session(
                customer_id=subscription.stripe_customer_id,
                organization_id=str(organization.id),
                plan=plan,
                plan_name=plan_configuration.get_plan_display(),
                amount=plan_configuration.amount,
                currency=plan_configuration.currency,
                billing_interval=plan_configuration.billing_interval,
                quantity=get_organization_seat_count(organization, plan),
            )
            checkout_url = str(checkout["url"])
        except (httpx.HTTPError, KeyError, ValueError):
            return Response(
                {"detail": "Stripe could not start checkout. Try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        subscription.plan = plan
        subscription.save()
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.BILLING_CHECKOUT,
            f"Started {subscription.get_plan_display()} checkout for {organization.name}.",
            actor=actor,
            target_type="organization",
            target_id=organization.id,
        )
        return Response({"checkout_url": checkout_url}, status=status.HTTP_201_CREATED)


class BillingPortalView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

    @extend_schema(request=None, responses=BillingPortalResponseSerializer)
    def post(self, request):
        actor, organization = get_request_actor_and_organization(request)
        require_billing_manager(actor, organization)
        subscription = get_organization_subscription(organization)
        if not settings.STRIPE_SECRET_KEY or not subscription.stripe_customer_id:
            return Response(
                {"detail": "No Stripe customer is available for this company."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            portal = StripeAPIClient().create_portal_session(
                customer_id=subscription.stripe_customer_id,
            )
            portal_url = str(portal["url"])
        except (httpx.HTTPError, KeyError, ValueError):
            return Response(
                {"detail": "Stripe could not open the billing portal. Try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.BILLING_PORTAL,
            f"Opened the billing portal for {organization.name}.",
            actor=actor,
            target_type="organization",
            target_id=organization.id,
        )
        return Response({"portal_url": portal_url})


class StripeWebhookView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(request=None, responses=StripeWebhookResponseSerializer)
    def post(self, request):
        raw_body = request.body
        signature = request.headers.get("Stripe-Signature", "")
        if not settings.STRIPE_WEBHOOK_SECRET:
            return Response({"detail": "Stripe webhooks are not configured."}, status=503)
        if not verify_stripe_signature(raw_body, signature, settings.STRIPE_WEBHOOK_SECRET):
            return Response({"detail": "Invalid Stripe signature."}, status=400)

        try:
            payload = json.loads(raw_body)
            event_id = str(payload["id"])
            event_type = str(payload["type"])
            event_object = payload["data"]["object"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return Response({"detail": "Invalid Stripe event payload."}, status=400)

        payload_sha256 = hashlib.sha256(raw_body).hexdigest()
        webhook_event, created = StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=event_id,
            defaults={
                "event_type": event_type,
                "payload_sha256": payload_sha256,
            },
        )
        if not created and webhook_event.payload_sha256 != payload_sha256:
            return Response({"detail": "Stripe event payload mismatch."}, status=400)
        if not created and webhook_event.status in {
            StripeWebhookEvent.ProcessingStatusEnum.PROCESSED,
            StripeWebhookEvent.ProcessingStatusEnum.IGNORED,
        }:
            return Response({"received": True, "duplicate": True})

        try:
            handled = False
            if event_type == "checkout.session.completed":
                metadata = event_object.get("metadata") if isinstance(event_object.get("metadata"), dict) else {}
                organization = Organization.objects.filter(pk=metadata.get("organization_id")).first()
                if organization:
                    subscription = get_organization_subscription(organization)
                    subscription.stripe_customer_id = str(
                        event_object.get("customer") or subscription.stripe_customer_id
                    )
                    subscription.stripe_subscription_id = str(
                        event_object.get("subscription") or subscription.stripe_subscription_id
                    )
                    subscription.plan = str(metadata.get("plan") or subscription.plan)
                    subscription.save()
                    if subscription.stripe_subscription_id and settings.STRIPE_SECRET_KEY:
                        stripe_subscription = StripeAPIClient().retrieve_subscription(
                            subscription.stripe_subscription_id
                        )
                        sync_subscription_object(stripe_subscription)
                    handled = True
            elif event_type in {
                "customer.subscription.created",
                "customer.subscription.updated",
                "customer.subscription.deleted",
            }:
                handled = sync_subscription_object(event_object)

            webhook_event.status = (
                StripeWebhookEvent.ProcessingStatusEnum.PROCESSED
                if handled
                else StripeWebhookEvent.ProcessingStatusEnum.IGNORED
            )
            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["status", "processed_at"])
        except Exception:
            webhook_event.status = StripeWebhookEvent.ProcessingStatusEnum.FAILED
            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["status", "processed_at"])
            raise

        return Response({"received": True})
