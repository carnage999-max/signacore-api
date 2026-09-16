from __future__ import annotations

from rest_framework.throttling import ScopedRateThrottle


class SignacoreRateThrottle(ScopedRateThrottle):
    """Rate-limit requests by the address assigned by the trusted proxy."""

    def get_cache_key(self, request, view):
        if not self.scope:
            return None

        real_ip = str(request.META.get("HTTP_X_REAL_IP", "")).strip()
        forwarded_ip = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).split(",", 1)[0].strip()
        ident = real_ip or forwarded_ip or str(request.META.get("REMOTE_ADDR", "")).strip()
        return self.cache_format % {"scope": self.scope, "ident": ident or "unknown"}
