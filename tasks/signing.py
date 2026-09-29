import logging

from celery import shared_task
from django.utils import timezone

from apps.signing.models import SigningRequest
from services.document_completion import documents_awaiting_their_copy, issue_completed_document
from tasks.notifications import send_completion_emails
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
    """Finish documents that everyone signed but which never got their completed copy.

    The copy is produced in the request carrying the last signature, and anything interrupting
    that leaves the signatures stored with nothing to show for them: no copy to download, and no
    completion email, because that is sent only once a copy exists. Nothing noticed, because
    nothing was watching. This is what watches.

    One document failing does not stop the others; it is logged and tried again next time.
    """
    issued = 0
    for document in documents_awaiting_their_copy():
        try:
            issue_completed_document(document)
        except Exception:
            logger.exception(
                "Could not issue completed document",
                extra={"document_id": str(document.id)},
            )
            continue

        enqueue_task(send_completion_emails, str(document.id))
        issued += 1

    if issued:
        logger.info("Issued %s completed document(s) that had been left without one", issued)
    return issued
