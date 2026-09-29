"""Error reporting.

SignaCore handles signatures, identity documents and the contents of other people's agreements, so
what is sent to a third party is decided here deliberately rather than left to a default. Personal
data is not attached to events, and the fields that would carry it are scrubbed before sending.

Reporting only starts when a DSN is configured, so development, tests and any deployment without
one behave exactly as they did before.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Values that identify a person or unlock an account. An event is more useful with the request that
# produced it, and none of these are needed to understand one.
SCRUBBED_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "csrfmiddlewaretoken",
        "email",
        "fernet_key",
        "otp",
        "otp_code",
        "password",
        "secret_key",
        "session_token",
        "signer_email",
        "signer_name",
        "text_value",
        "x-signacore-secret",
    }
)
SCRUBBED_PLACEHOLDER = "[scrubbed]"


def _scrub(value: Any, depth: int = 0) -> Any:
    """Replace anything keyed as sensitive, however deeply it is nested in the event."""
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: SCRUBBED_PLACEHOLDER if str(key).lower() in SCRUBBED_KEYS else _scrub(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, depth + 1) for item in value]
    return value


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Scrub an event on its way out."""
    del hint
    return _scrub(event)


def configure_error_reporting(
    *,
    dsn: str,
    environment: str,
    release: str,
    traces_sample_rate: float,
    debug: bool,
) -> bool:
    """Start error reporting, returning whether it was started.

    Called from settings, which is the only place that knows the configuration, and which runs
    before anything that might fail.
    """
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:  # pragma: no cover - the dependency is declared, so this is a broken install
        logger.warning("Error reporting is configured but the Sentry SDK is not installed")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release or None,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            # logger.exception already marks the places worth reporting, so those become events
            # rather than needing a second call beside each one.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        # Never attach the user, their address or the body of a request. A signed document's
        # contents are the customer's, not ours to send anywhere.
        send_default_pii=False,
        traces_sample_rate=traces_sample_rate,
        before_send=before_send,
        debug=debug,
    )
    return True
