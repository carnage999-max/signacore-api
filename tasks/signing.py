import logging

from celery import shared_task
from django.utils import timezone

from apps.signing.models import SigningRequest
from services.document_completion import (
    issue_signed_copy,
    refresh_document_status,
    signers_awaiting_their_copy,
)
from tasks.notifications import send_signed_copy
from utils.task_dispatch import enqueue_task

logger = logging.getLogger(__name__)


@shared_task(name="tasks.signing.expire_signing_links")
def expire_signing_links() -> None:
    now = timezone.now()
    SigningRequest.objects.filter(
        status__in=[
            SigningRequest.StatusEnum.PENDING,
            SigningRequest.StatusEnum.OTP_VERIFIED,
        ],
        expires_at__lte=now,
    ).update(
        status=SigningRequest.StatusEnum.EXPIRED,
        updated_at=now,
    )
    return None


@shared_task(name="tasks.signing.issue_outstanding_completed_documents")
def issue_outstanding_completed_documents() -> int:
    """Make the copies of signers who finished but whose copy was never produced.

    A copy is flattened in the request that carries the signature, and anything interrupting that
    leaves the signature stored with nothing to show for it: nothing to download, and no email,
    because the email carries the copy. Nothing noticed, because nothing was watching. This is
    what watches.

    One failing does not stop the others; it is logged and tried again next time.
    """
    issued = 0
    for signing_request in signers_awaiting_their_copy():
        try:
            issue_signed_copy(signing_request)
        except Exception:
            logger.exception(
                "Could not issue a signer's copy",
                extra={
                    "document_id": str(signing_request.document_id),
                    "signing_request_id": str(signing_request.id),
                },
            )
            continue

        refresh_document_status(signing_request.document)
        enqueue_task(send_signed_copy, str(signing_request.id))
        issued += 1

    if issued:
        logger.info("Issued %s signed copy(ies) that had been left unmade", issued)
    return issued
