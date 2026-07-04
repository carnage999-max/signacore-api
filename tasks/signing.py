from celery import shared_task


@shared_task(name="tasks.signing.expire_signing_links")
def expire_signing_links() -> None:
    return None

