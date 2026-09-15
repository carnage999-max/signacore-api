from __future__ import annotations

from calendar import monthrange
from datetime import datetime
from enum import Enum

from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import OrganizationSubscription


class PlanFeatureEnum(str, Enum):
    DOCUMENT_CREATE = "DOCUMENT_CREATE"
    SIGNING_REQUEST_SEND = "SIGNING_REQUEST_SEND"
    COMPLETED_ARCHIVE = "COMPLETED_ARCHIVE"
    AUDIT_HISTORY = "AUDIT_HISTORY"
    TEAM_MANAGEMENT = "TEAM_MANAGEMENT"


class BillingErrorCodeEnum(str, Enum):
    PLAN_FEATURE_REQUIRED = "PLAN_FEATURE_REQUIRED"
    FREE_MONTHLY_LIMIT_REACHED = "FREE_MONTHLY_LIMIT_REACHED"


class BillingMessageEnum(str, Enum):
    PLAN_FEATURE_REQUIRED = "This action is not included in your current plan. Upgrade to continue."
    FREE_MONTHLY_LIMIT_REACHED = "Your Free plan allows five documents per month. Upgrade for unlimited document sending."


FREE_DOCUMENT_LIMIT = 5
PAID_SUBSCRIPTION_STATUSES = frozenset(
    {
        OrganizationSubscription.StatusEnum.ACTIVE,
        OrganizationSubscription.StatusEnum.TRIALING,
    }
)

PLAN_FEATURES: dict[str, frozenset[PlanFeatureEnum]] = {
    OrganizationSubscription.PlanEnum.FREE: frozenset(
        {
            PlanFeatureEnum.DOCUMENT_CREATE,
            PlanFeatureEnum.SIGNING_REQUEST_SEND,
            PlanFeatureEnum.COMPLETED_ARCHIVE,
        }
    ),
    OrganizationSubscription.PlanEnum.PROFESSIONAL: frozenset(
        {
            PlanFeatureEnum.DOCUMENT_CREATE,
            PlanFeatureEnum.SIGNING_REQUEST_SEND,
            PlanFeatureEnum.COMPLETED_ARCHIVE,
            PlanFeatureEnum.AUDIT_HISTORY,
        }
    ),
    OrganizationSubscription.PlanEnum.BUSINESS: frozenset(
        {
            PlanFeatureEnum.DOCUMENT_CREATE,
            PlanFeatureEnum.SIGNING_REQUEST_SEND,
            PlanFeatureEnum.COMPLETED_ARCHIVE,
            PlanFeatureEnum.AUDIT_HISTORY,
            PlanFeatureEnum.TEAM_MANAGEMENT,
        }
    ),
    OrganizationSubscription.PlanEnum.ENTERPRISE: frozenset(PlanFeatureEnum),
}


def _month_window() -> tuple[datetime, datetime]:
    now = timezone.localtime()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = monthrange(now.year, now.month)[1]
    end = start.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def get_effective_plan(organization, *, actor=None, subscription=None) -> str:
    if actor is not None and actor.is_superuser:
        return OrganizationSubscription.PlanEnum.ENTERPRISE

    subscription = subscription or OrganizationSubscription.objects.filter(organization=organization).first()
    if subscription is None:
        return OrganizationSubscription.PlanEnum.FREE
    if subscription.plan in {
        OrganizationSubscription.PlanEnum.PROFESSIONAL,
        OrganizationSubscription.PlanEnum.BUSINESS,
        OrganizationSubscription.PlanEnum.ENTERPRISE,
    } and subscription.status in PAID_SUBSCRIPTION_STATUSES:
        return subscription.plan
    return OrganizationSubscription.PlanEnum.FREE


def get_monthly_document_usage(organization) -> dict[str, int]:
    from apps.documents.models import Document
    from apps.signing.models import SigningRequest

    start, end = _month_window()
    created = Document.objects.filter(
        organization=organization,
        created_at__gte=start,
        created_at__lte=end,
    ).count()
    sent = SigningRequest.objects.filter(
        document__organization=organization,
        created_at__gte=start,
        created_at__lte=end,
    ).values("document_id").distinct().count()
    return {"created": created, "sent": sent}


def get_entitlement_snapshot(organization, *, actor=None, subscription=None) -> dict:
    effective_plan = get_effective_plan(organization, actor=actor, subscription=subscription)
    usage = get_monthly_document_usage(organization)
    features = PLAN_FEATURES[effective_plan]
    free_limit = FREE_DOCUMENT_LIMIT if effective_plan == OrganizationSubscription.PlanEnum.FREE else None
    return {
        "effective_plan": effective_plan,
        "monthly_document_limit": free_limit,
        "monthly_documents_created": usage["created"],
        "monthly_documents_sent": usage["sent"],
        "features": {
            feature.value: {
                "enabled": feature in features,
                "limit": (
                    free_limit
                    if feature == PlanFeatureEnum.DOCUMENT_CREATE
                    else free_limit if feature == PlanFeatureEnum.SIGNING_REQUEST_SEND else None
                ),
                "used": (
                    usage["created"]
                    if feature == PlanFeatureEnum.DOCUMENT_CREATE
                    else usage["sent"] if feature == PlanFeatureEnum.SIGNING_REQUEST_SEND else None
                ),
                "upgrade_plan": (
                    OrganizationSubscription.PlanEnum.BUSINESS
                    if feature == PlanFeatureEnum.TEAM_MANAGEMENT
                    else OrganizationSubscription.PlanEnum.PROFESSIONAL
                ) if feature not in features else None,
            }
            for feature in PlanFeatureEnum
        },
    }


class BillingEntitlementError(APIException):
    status_code = 403
    default_code = BillingErrorCodeEnum.PLAN_FEATURE_REQUIRED.value

    def __init__(self, code: BillingErrorCodeEnum, message: BillingMessageEnum, *, plan: str, feature: PlanFeatureEnum, upgrade_plan: str):
        self.detail = {
            "code": code.value,
            "message": message.value,
            "plan": plan,
            "feature": feature.value,
            "upgrade_plan": upgrade_plan,
        }


def require_feature(actor, organization, feature: PlanFeatureEnum) -> str:
    effective_plan = get_effective_plan(organization, actor=actor)
    if feature in PLAN_FEATURES[effective_plan]:
        return effective_plan
    upgrade_plan = (
        OrganizationSubscription.PlanEnum.BUSINESS
        if feature == PlanFeatureEnum.TEAM_MANAGEMENT
        else OrganizationSubscription.PlanEnum.PROFESSIONAL
    )
    raise BillingEntitlementError(
        BillingErrorCodeEnum.PLAN_FEATURE_REQUIRED,
        BillingMessageEnum.PLAN_FEATURE_REQUIRED,
        plan=effective_plan,
        feature=feature,
        upgrade_plan=upgrade_plan,
    )


def require_monthly_document_capacity(actor, organization, *, operation: str, document=None) -> None:
    effective_plan = require_feature(
        actor,
        organization,
        PlanFeatureEnum.DOCUMENT_CREATE if operation == "create" else PlanFeatureEnum.SIGNING_REQUEST_SEND,
    )
    if effective_plan != OrganizationSubscription.PlanEnum.FREE:
        return

    usage = get_monthly_document_usage(organization)
    used = usage["created"] if operation == "create" else usage["sent"]
    if operation == "send" and document is not None:
        from apps.signing.models import SigningRequest

        start, end = _month_window()
        already_counted = SigningRequest.objects.filter(
            document=document,
            created_at__gte=start,
            created_at__lte=end,
        ).exists()
        if already_counted:
            return
    if used < FREE_DOCUMENT_LIMIT:
        return
    raise BillingEntitlementError(
        BillingErrorCodeEnum.FREE_MONTHLY_LIMIT_REACHED,
        BillingMessageEnum.FREE_MONTHLY_LIMIT_REACHED,
        plan=effective_plan,
        feature=(
            PlanFeatureEnum.DOCUMENT_CREATE
            if operation == "create"
            else PlanFeatureEnum.SIGNING_REQUEST_SEND
        ),
        upgrade_plan=OrganizationSubscription.PlanEnum.PROFESSIONAL,
    )
