from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.permissions import BasePermission


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
