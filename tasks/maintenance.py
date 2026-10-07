import logging

from celery import shared_task

from services.storage_cleanup import run_cleanup

logger = logging.getLogger(__name__)


@shared_task(name="tasks.maintenance.clean_up")
def clean_up() -> dict:
    """Sweep what accumulates: expired codes, leaked files, and documents past retention.

    Retention does nothing until SIGNACORE_DOCUMENT_RETENTION_DAYS is set, so running this on a
    fresh deployment reclaims leaked files and forgets expired codes and deletes nothing else.
    """
    report = run_cleanup()
    if report.touched_anything:
        logger.info("Cleanup ran", extra=report.as_dict())
    return report.as_dict()
