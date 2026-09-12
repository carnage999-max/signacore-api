from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.permissions import BasePermission

from apps.accounts.models import AccountProfile, Organization, OrganizationMembership


class HasValidSignacoreSecret(BasePermission):
    message = "Invalid Signacore secret."

    def has_permission(self, request, view) -> bool:
        if request.method == "OPTIONS":
            return True

        expected_secret = getattr(settings, "SIGNACORE_SHARED_SECRET", "")
        if not expected_secret:
            return False

        provided_secret = request.headers.get("X-Signacore-Secret", "")
        return provided_secret == expected_secret


def get_admin_actor(request):
    actor_id = request.headers.get("X-Signacore-Admin-Id", "").strip()
    if not actor_id:
        return None

    return (
        get_user_model()
        .objects.filter(pk=actor_id, is_staff=True, is_active=True)
        .first()
    )


def require_superuser_actor(request):
    actor = get_admin_actor(request)
    return actor if actor and actor.is_superuser else None


def ensure_actor_organization(actor) -> Organization | None:
    membership = (
        OrganizationMembership.objects.select_related("organization")
        .filter(
            user=actor,
            status=OrganizationMembership.StatusEnum.ACTIVE,
            organization__status=Organization.StatusEnum.ACTIVE,
        )
        .order_by("created_at")
        .first()
    )
    if membership:
        return membership.organization

    if not actor.is_superuser and actor.username != settings.SIGNACORE_SERVICE_USERNAME:
        return None

    with transaction.atomic():
        organization = Organization.objects.filter(status=Organization.StatusEnum.ACTIVE).first()
        if organization is None:
            organization = Organization.objects.create(
                name="Se7en Inc." if actor.is_superuser else "SignaCore Internal",
                created_by=actor,
            )
        OrganizationMembership.objects.get_or_create(
            organization=organization,
            user=actor,
            defaults={
                "role": (
                    OrganizationMembership.RoleEnum.OWNER
                    if actor.is_superuser
                    else OrganizationMembership.RoleEnum.ADMIN
                ),
                "status": OrganizationMembership.StatusEnum.ACTIVE,
            },
        )
        AccountProfile.objects.get_or_create(
            user=actor,
            defaults={
                "account_type": (
                    AccountProfile.AccountTypeEnum.PLATFORM
                    if actor.is_superuser
                    else AccountProfile.AccountTypeEnum.COMPANY
                ),
                "email": actor.email or "",
                "display_name": actor.get_full_name() or actor.username,
            },
        )
    return organization


def get_actor_organization(request, actor=None) -> Organization | None:
    resolved_actor = actor or get_admin_actor(request)
    if resolved_actor is None:
        return None

    requested_id = request.headers.get("X-Signacore-Organization-Id", "").strip()
    if requested_id:
        organizations = Organization.objects.filter(
            pk=requested_id,
            status=Organization.StatusEnum.ACTIVE,
        )
        if resolved_actor.is_superuser:
            return organizations.first()
        return organizations.filter(
            memberships__user=resolved_actor,
            memberships__status=OrganizationMembership.StatusEnum.ACTIVE,
        ).first()

    return ensure_actor_organization(resolved_actor)
