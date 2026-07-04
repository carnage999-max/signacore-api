from celery import shared_task


@shared_task(name="tasks.notifications.send_invitation_emails")
def send_invitation_emails(document_id: str) -> None:
    return None


@shared_task(name="tasks.notifications.send_completion_emails")
def send_completion_emails(document_id: str) -> None:
    return None


@shared_task(name="tasks.notifications.send_otp_email")
def send_otp_email(signing_request_id: str) -> None:
    return None


@shared_task(name="tasks.notifications.notify_admin_progress")
def notify_admin_progress(document_id: str, signing_request_id: str) -> None:
    return None

