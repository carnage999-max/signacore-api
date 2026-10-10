"""Writing the trail behind a signature.

Every call here appends. Nothing updates or deletes an event, and nothing should: a record that
can be tidied up afterwards is worth very little when somebody is disputing what happened.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.signing.models import SigningEvent, SigningRequest
from utils.request_meta import get_client_ip, get_user_agent

# Opening a link is not a deliberate act the way entering a code is. A mail client preview pane
# or a corporate link scanner fetches the page without anybody having read a word, and a signer
# who leaves a tab open generates a request every time it refreshes. Repeat opens from the same
# address inside this window collapse into the first, so the trail records occasions rather than
# HTTP requests.
#
# Deliberately keyed on the address alone. Including the device would be more precise about two
# people sharing an office connection, and would also let anyone holding the link write sixty
# rows a minute into somebody else's history by varying their user agent - this endpoint is open
# by necessity, since a signer has no account. The device is still recorded on the row; it just
# does not decide whether the row is written.
OPEN_DEDUPE_MINUTES = 15


def record_signing_event(
    signing_request: SigningRequest,
    event: str,
    *,
    request=None,
    detail: str = "",
    at=None,
) -> SigningEvent | None:
    """Append one event. Returns None when the event was folded into a recent identical one."""
    ip_address = get_client_ip(request) if request is not None else ""
    user_agent = get_user_agent(request) if request is not None else ""
    moment = at or timezone.now()

    if event == SigningEvent.EventEnum.OPENED and _opened_recently(signing_request, ip_address, moment):
        return None

    return SigningEvent.objects.create(
        signing_request=signing_request,
        event=event,
        at=moment,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=detail,
    )


def _opened_recently(signing_request: SigningRequest, ip_address: str, moment) -> bool:
    since = moment - timedelta(minutes=OPEN_DEDUPE_MINUTES)
    # Compared in Python rather than in the query: ip_address is encrypted with Fernet, which is
    # non-deterministic, so the same address does not produce the same ciphertext twice and the
    # database cannot match on it at all. The time window keeps the number of rows read small.
    recent = signing_request.events.filter(
        event=SigningEvent.EventEnum.OPENED,
        at__gte=since,
    ).order_by(
        "-at"
    )[:20]
    return any(event.ip_address == ip_address for event in recent)
