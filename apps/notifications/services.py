from django.conf import settings
from django.core.mail import EmailMessage


def send_email(subject: str, body: str, recipients: list[str], attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    for attachment in attachments or []:
        message.attach(*attachment)
    message.send(fail_silently=False)

