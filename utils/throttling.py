from __future__ import annotations

from rest_framework.throttling import ScopedRateThrottle

# Scopes where the request already names the document it concerns. Everything else - asking for a
# code, offering one, signing in - is identified by address alone, because those are the limits
# that stand in front of an unauthenticated caller and must not be divisible.
PER_SIGNING_REQUEST_SCOPES = frozenset(
    {
        "signer_context",
        "signer_preview",
        "signer_submit",
    }
)


class SignacoreRateThrottle(ScopedRateThrottle):
    """Rate-limit by the address assigned by the trusted proxy, and by document where there is one.

    Address alone was the whole identity, which made the limit a property of the network rather
    than of the person using it. Several people signing from one office share one address, so they
    shared one allowance: colleagues sent the same employment pack would take each other's budget
    and see the rest of their documents as broken images.

    Where the URL names a signing request, that is added to the key, so one person reading their
    document cannot exhaust another's. It cannot be used to escape the limit: a caller inventing
    tokens gets a bucket of its own for each, but an unknown token is a 404 before anything is
    read or drawn, and the expensive paths need both a real token and the verified session that
    only an emailed code issues.

    Deliberately not the session cookie, which the throttle cannot verify without the database
    lookup it runs ahead of - an unverified cookie as the key would let anyone mint a fresh
    allowance per request simply by changing it.
    """

    def get_cache_key(self, request, view):
        if not self.scope:
            return None

        return self.cache_format % {"scope": self.scope, "ident": self.identify(request, view)}

    def identify(self, request, view) -> str:
        address = self.address_of(request)
        if self.scope not in PER_SIGNING_REQUEST_SCOPES:
            return address

        token = str(getattr(view, "kwargs", {}).get("token", "")).strip()
        return f"{address}|{token}" if token else address

    @staticmethod
    def address_of(request) -> str:
        real_ip = str(request.META.get("HTTP_X_REAL_IP", "")).strip()
        forwarded_ip = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).split(",", 1)[0].strip()
        return real_ip or forwarded_ip or str(request.META.get("REMOTE_ADDR", "")).strip() or "unknown"
