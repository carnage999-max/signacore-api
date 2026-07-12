from django.conf import settings
from django.core.mail import EmailMessage

from apps.documents.models import Document
from apps.signing.models import SigningRequest


def send_email(subject: str, body: str, recipients: list[str], attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    if not recipients:
        return

    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    for attachment in attachments or []:
        message.attach(*attachment)
    message.send(fail_silently=False)


def build_signing_link(signing_request: SigningRequest) -> str:
    base_url = settings.SIGNING_LINK_BASE_URL.rstrip("/")
    return f"{base_url}/sign/{signing_request.id}/"


def send_invitation_email(signing_request: SigningRequest) -> None:
    signer_name = signing_request.signer_name or "there"
    subject = f"Signature requested: {signing_request.document.title}"
    body = (
        f"Hello {signer_name},\n\n"
        "Se7en Inc. sent you a document to review and sign in Signacore.\n\n"
        f"Document: {signing_request.document.title}\n"
        f"Signing link: {build_signing_link(signing_request)}\n"
        f"Link expires: {signing_request.expires_at:%Y-%m-%d %H:%M %Z}\n\n"
        "If you were not expecting this request, ignore this email.\n"
    )
    send_email(subject, body, [signing_request.signer_email])


def send_otp_email_message(signing_request: SigningRequest, otp_code: str) -> None:
    signer_name = signing_request.signer_name or "there"
    subject = f"Your Signacore verification code for {signing_request.document.title}"
    body = (
        f"Hello {signer_name},\n\n"
        "Use the code below to continue signing your document in Signacore.\n\n"
        f"OTP code: {otp_code}\n"
        f"Expires in: {settings.OTP_EXPIRY_MINUTES} minutes\n\n"
        "If you did not request this code, ignore this email.\n"
    )
    send_email(subject, body, [signing_request.signer_email])


def send_completion_email(document: Document) -> None:
    if not document.signed_pdf:
        return

    recipients = list(
        dict.fromkeys(
            [
                *(request.signer_email for request in document.signing_requests.all()),
                getattr(document.created_by, "email", "") or "",
            ]
        )
    )
    recipients = [recipient for recipient in recipients if recipient]
    if not recipients:
        return

    with document.signed_pdf.open("rb") as signed_pdf_handle:
        attachment_bytes = signed_pdf_handle.read()

    subject = f"Completed document: {document.title}"
    body = (
        "The document below has been fully signed in Signacore.\n\n"
        f"Document: {document.title}\n"
        "The completed PDF is attached.\n"
    )
    send_email(
        subject,
        body,
        recipients,
        attachments=[
            (
                f"{document.title.replace(' ', '-').lower()}-signed.pdf",
                attachment_bytes,
                "application/pdf",
            )
        ],
    )


def send_progress_email(document: Document, signing_request: SigningRequest) -> None:
    admin_email = getattr(document.created_by, "email", "") or ""
    if not admin_email:
        return

    signer_name = signing_request.signer_name or signing_request.signer_email
    subject = f"Signing progress update: {document.title}"
    body = (
        "A signer completed their portion of a Signacore document.\n\n"
        f"Document: {document.title}\n"
        f"Signer: {signer_name}\n"
        f"Status: {document.status}\n"
    )
    send_email(subject, body, [admin_email])
