from celery import shared_task
from django.utils import timezone

from apps.signing.models import SigningRequest


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
