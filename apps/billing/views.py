from __future__ import annotations

import hashlib
import json
import logging
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
from tasks.notifications import send_subscription_activated
from utils.task_dispatch import enqueue_task

from .models import BillingPlanConfiguration, OrganizationSubscription, StripeWebhookEvent
from .serializers import (
    BillingPlanConfigurationSerializer,
    BillingPortalResponseSerializer,
    CheckoutSessionResponseSerializer,
    CheckoutSessionSerializer,
    CheckoutSessionStatusSerializer,
    OrganizationSubscriptionSerializer,
    StripeWebhookResponseSerializer,
)

logger = logging.getLogger(__name__)


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


def sync_subscription_object(payload: dict, *, notify: bool = True) -> bool:
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
    previous_status = subscription.status
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
    if (
        notify
        and subscription.status
        in {
            OrganizationSubscription.StatusEnum.ACTIVE,
            OrganizationSubscription.StatusEnum.TRIALING,
        }
        and previous_status
        not in {
            OrganizationSubscription.StatusEnum.ACTIVE,
            OrganizationSubscription.StatusEnum.TRIALING,
        }
    ):
        try:
            enqueue_task(send_subscription_activated, str(organization.id))
        except Exception:
            logger.exception(
                "Subscription activation email dispatch failed",
                extra={"organization_id": str(organization.id)},
            )
    return True


def sync_checkout_session(session: dict, organization: Organization) -> bool:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    if str(metadata.get("organization_id") or "") != str(organization.id):
        return False

    subscription = get_organization_subscription(organization)
    customer_id = str(session.get("customer") or "")
    if subscription.stripe_customer_id and customer_id != subscription.stripe_customer_id:
        return False

    session_status = str(session.get("status") or "").lower()
    payment_status = str(session.get("payment_status") or "").lower()
    if session_status != "complete" or payment_status not in {"paid", "no_payment_required"}:
        return False

    subscription_id = str(session.get("subscription") or "")
    if not customer_id or not subscription_id:
        return False

    plan = str(metadata.get("plan") or "").upper()
    valid_plans = {value for value, _ in OrganizationSubscription.PlanEnum.choices}
    if plan not in valid_plans:
        return False

    subscription.stripe_customer_id = customer_id
    subscription.stripe_subscription_id = subscription_id
    subscription.plan = plan
    subscription.save(update_fields=["stripe_customer_id", "stripe_subscription_id", "plan", "updated_at"])
    stripe_subscription = StripeAPIClient().retrieve_subscription(subscription_id)
    return sync_subscription_object(stripe_subscription)


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
        if subscription.stripe_subscription_id and settings.STRIPE_SECRET_KEY:
            try:
                stripe_subscription = StripeAPIClient().retrieve_subscription(subscription.stripe_subscription_id)
                sync_subscription_object(stripe_subscription)
                subscription.refresh_from_db()
            except (httpx.HTTPError, KeyError, ValueError):
                # The webhook snapshot remains usable if Stripe is temporarily unavailable.
                logger.warning(
                    "Stripe subscription reconciliation failed",
                    extra={
                        "organization_id": str(organization.id),
                        "subscription_id": subscription.stripe_subscription_id,
                    },
                    exc_info=True,
                )
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


class BillingCheckoutStatusView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = OrganizationSubscriptionSerializer

    @extend_schema(request=CheckoutSessionStatusSerializer, responses=OrganizationSubscriptionSerializer)
    def post(self, request):
        actor, organization = get_request_actor_and_organization(request)
        require_billing_manager(actor, organization)
        serializer = CheckoutSessionStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session_id = serializer.validated_data["session_id"]
        subscription = get_organization_subscription(organization)

        if not settings.STRIPE_SECRET_KEY:
            return Response(
                {"detail": "Stripe billing is not configured on this server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            session = StripeAPIClient().retrieve_checkout_session(session_id)
            if not sync_checkout_session(session, organization):
                return Response(
                    {"detail": "Stripe has not confirmed this payment for the current workspace."},
                    status=status.HTTP_409_CONFLICT,
                )
            subscription.refresh_from_db()
        except httpx.HTTPError:
            logger.warning(
                "Stripe Checkout Session verification failed",
                extra={"organization_id": str(organization.id), "checkout_session_id": session_id},
                exc_info=True,
            )
            return Response(
                {"detail": "Stripe could not confirm this payment yet. Refresh billing in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except (KeyError, ValueError):
            logger.warning(
                "Stripe Checkout Session response was invalid",
                extra={"organization_id": str(organization.id), "checkout_session_id": session_id},
                exc_info=True,
            )
            return Response(
                {"detail": "Stripe returned an incomplete payment confirmation. Refresh billing in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.BILLING_CHECKOUT,
            f"Confirmed {subscription.get_plan_display()} checkout for {organization.name}.",
            actor=actor,
            target_type="organization",
            target_id=organization.id,
            metadata={"checkout_session_id": session_id},
        )
        return Response(OrganizationSubscriptionSerializer(subscription, context={"actor": actor}).data)


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
            logger.info(
                "Ignoring duplicate Stripe webhook", extra={"stripe_event_id": event_id, "event_type": event_type}
            )
            return Response({"received": True, "duplicate": True})

        logger.info("Stripe webhook received", extra={"stripe_event_id": event_id, "event_type": event_type})

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
            if not handled:
                logger.warning(
                    "Stripe webhook did not match a SignaCore workspace",
                    extra={"stripe_event_id": event_id, "event_type": event_type},
                )
        except Exception:
            logger.exception(
                "Stripe webhook processing failed",
                extra={"stripe_event_id": event_id, "event_type": event_type},
            )
            webhook_event.status = StripeWebhookEvent.ProcessingStatusEnum.FAILED
            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["status", "processed_at"])
            raise

        return Response({"received": True})
