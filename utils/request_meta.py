"""Who a request came from, as far as the edge can tell us.

One implementation. There were two, identical and separate, which is the kind of duplication
that stays identical right up until somebody fixes a bug in one of them.

Everything here is only as trustworthy as the proxy in front of it. ``X-Real-IP`` is set by
nginx from the connection it accepted, so a client cannot forge it; ``X-Forwarded-For`` can be
appended to by the client and is read only when the first header is absent. If SignaCore is ever
deployed without a proxy that sets ``X-Real-IP``, the address recorded against a signature
becomes a claim the signer made about themselves rather than an observation, and the deployment
notes say so.
"""

from __future__ import annotations

# Long enough for any real browser string, short enough that nobody can use the field as storage.
MAX_USER_AGENT_LENGTH = 400


def get_client_ip(request) -> str:
    real_ip = str(request.META.get("HTTP_X_REAL_IP", "")).strip()
    if real_ip:
        return real_ip

    forwarded_for = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return str(request.META.get("REMOTE_ADDR", "")).strip()


def get_user_agent(request) -> str:
    return str(request.META.get("HTTP_USER_AGENT", "")).strip()[:MAX_USER_AGENT_LENGTH]
