from __future__ import annotations

from django.conf import settings
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
