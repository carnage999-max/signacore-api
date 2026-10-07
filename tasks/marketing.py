"""The one email SignaCore sends about itself.

Every signed document puts SignaCore in front of somebody who has never heard of it, which is the
cheapest reach the product has and the only one it already owns. This spends it once, a day later,
and then never again for that person.

It is off unless SIGNACORE_SIGNER_FOLLOW_UP_ENABLED is set, because it is the only outbound mail
that is not about a document the recipient is already expecting, and the sending domain's standing
is worth more than a day's head start.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.notifications.models import EmailSuppression
from apps.notifications.services import send_signer_follow_up_email
from apps.signing.models import SigningRequest

logger = logging.getLogger(__name__)

# A day after signing: long enough not to crowd the copy they actually wanted, soon enough that
# they remember using it.
FOLLOW_UP_AFTER_HOURS = 24
# And not at all if we are late. An email about something somebody did last week reads as a list,
# not as a follow-up.
FOLLOW_UP_WINDOW_HOURS = 96


def follow_ups_due(*, now=None):
    now = now or timezone.now()
    return SigningRequest.objects.filter(
        status=SigningRequest.StatusEnum.SIGNED,
        follow_up_sent_at__isnull=True,
        signed_at__lte=now - timedelta(hours=FOLLOW_UP_AFTER_HOURS),
        signed_at__gte=now - timedelta(hours=FOLLOW_UP_WINDOW_HOURS),
    ).select_related("document")


@shared_task(name="tasks.marketing.send_signer_follow_ups")
def send_signer_follow_ups() -> int:
    if not getattr(settings, "SIGNACORE_SIGNER_FOLLOW_UP_ENABLED", False):
        return 0

    sent = 0
    for signing_request in follow_ups_due():
        email = signing_request.signer_email
        # Once per person, not once per document. Somebody who signs ten things hears from us once.
        already_pitched = (
            SigningRequest.objects.filter(
                signer_email_hash=signing_request.signer_email_hash,
                follow_up_sent_at__isnull=False,
            )
            .exclude(pk=signing_request.pk)
            .exists()
        )
        if already_pitched or EmailSuppression.is_suppressed(email):
            # Marked as handled either way, so the query does not keep finding it.
            signing_request.follow_up_sent_at = timezone.now()
            signing_request.save(update_fields=["follow_up_sent_at", "updated_at"])
            continue

        try:
            send_signer_follow_up_email(signing_request)
        except Exception:
            logger.exception(
                "Could not send a signer follow-up",
                extra={"signing_request_id": str(signing_request.id)},
            )
            continue

        signing_request.follow_up_sent_at = timezone.now()
        signing_request.save(update_fields=["follow_up_sent_at", "updated_at"])
        sent += 1

    if sent:
        logger.info("Sent %s signer follow-up(s)", sent)
    return sent
