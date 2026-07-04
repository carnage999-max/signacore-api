from celery import shared_task


@shared_task(name="tasks.documents.analyse_document_fields")
def analyse_document_fields(document_id: str) -> None:
    return None


@shared_task(name="tasks.documents.flatten_document")
def flatten_document(document_id: str) -> None:
    return None

