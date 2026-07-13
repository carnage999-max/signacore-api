from celery import shared_task

from apps.documents.models import Document
from apps.notifications.services import (
    send_completion_email,
    send_invitation_email,
    send_otp_email_message,
    send_progress_email,
)
from apps.signing.models import SigningRequest


@shared_task(name="tasks.notifications.send_invitation_emails")
def send_invitation_emails(document_id: str) -> None:
    document = (
        Document.objects.select_related("created_by")
        .prefetch_related("signing_requests")
        .filter(pk=document_id)
        .first()
    )
    if document is None:
        return None

    for signing_request in document.signing_requests.all():
        send_invitation_email(signing_request)
    return None


@shared_task(name="tasks.notifications.send_invitation_email_for_request")
def send_invitation_email_for_request(signing_request_id: str) -> None:
    signing_request = (
        SigningRequest.objects.select_related("document")
        .filter(pk=signing_request_id)
        .first()
    )
    if signing_request is None:
        return None

    send_invitation_email(signing_request)
    return None


@shared_task(name="tasks.notifications.send_completion_emails")
def send_completion_emails(document_id: str) -> None:
    document = (
        Document.objects.select_related("created_by")
        .prefetch_related("signing_requests")
        .filter(pk=document_id)
        .first()
    )
    if document is None:
        return None

    send_completion_email(document)
    return None


@shared_task(name="tasks.notifications.send_otp_email")
def send_otp_email(signing_request_id: str, otp_code: str) -> None:
    signing_request = (
        SigningRequest.objects.select_related("document")
        .filter(pk=signing_request_id)
        .first()
    )
    if signing_request is None:
        return None

    send_otp_email_message(signing_request, otp_code)
    return None


@shared_task(name="tasks.notifications.notify_admin_progress")
def notify_admin_progress(document_id: str, signing_request_id: str) -> None:
    document = Document.objects.select_related("created_by").filter(pk=document_id).first()
    signing_request = (
        SigningRequest.objects.select_related("document")
        .filter(pk=signing_request_id)
        .first()
    )
    if document is None or signing_request is None:
        return None

    send_progress_email(document, signing_request)
    return None
