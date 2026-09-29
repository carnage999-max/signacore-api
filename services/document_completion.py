"""Issuing the completed copy of a fully signed document.

This runs from two places: the request that carries the last signature, and the reconciler that
sweeps up documents whose last signature did not produce a copy. Both need the same thing to
happen, so neither owns it.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack

from apps.documents.models import Document
from services.pdf_engine import PDFEngine
from utils.file_storage import save_encrypted_field_file, temporary_output_file, temporary_plaintext_file

logger = logging.getLogger(__name__)


class CompletionNotReady(Exception):
    """The document cannot be completed yet, and nothing is wrong."""


def every_signature_is_in(document: Document) -> bool:
    """Check that the document has signers and that none of them is still outstanding."""
    from apps.signing.models import SigningRequest

    requests = document.signing_requests.all()
    if not requests:
        return False
    return not any(request.status != SigningRequest.StatusEnum.SIGNED for request in requests)


def issue_completed_document(document: Document) -> None:
    """Flatten every submission onto the document and store the completed copy.

    Raises ``services.pdf_sanitizer.PDFSanitizationError`` when the result still carries active
    content, in which case
    nothing is stored and the document is left as it was, so a caller can decide whether to tell
    a signer or simply try again later.
    """
    from apps.signing.models import FieldSubmission

    if not every_signature_is_in(document):
        raise CompletionNotReady("A signer has still to sign this document.")

    submissions = list(
        FieldSubmission.objects.select_related("document_field")
        .filter(signing_request__document=document)
        .order_by("submitted_at")
    )

    with ExitStack() as stack:
        source_path = stack.enter_context(temporary_plaintext_file(document.original_pdf, suffix=".pdf"))
        output_path = stack.enter_context(temporary_output_file(suffix=".pdf"))
        flatten_submissions = []
        for submission in submissions:
            image_path = ""
            if submission.image_value:
                image_path = str(stack.enter_context(temporary_plaintext_file(submission.image_value, suffix=".png")))
            flatten_submissions.append(
                {
                    "page": submission.document_field.page,
                    "field_type": submission.document_field.field_type,
                    "max_length": submission.document_field.max_length,
                    "is_comb": submission.document_field.is_comb,
                    "x": submission.document_field.x,
                    "y": submission.document_field.y,
                    "width": submission.document_field.width,
                    "height": submission.document_field.height,
                    "value_type": submission.value_type,
                    "text_value": submission.text_value,
                    "image_path": image_path,
                }
            )

        PDFEngine().flatten(source_path, output_path, flatten_submissions)
        save_encrypted_field_file(document.signed_pdf, output_path, filename=f"{document.id}-signed.pdf")

    document.status = Document.StatusEnum.COMPLETED
    document.save(update_fields=["status", "signed_pdf", "updated_at"])


def documents_awaiting_their_copy() -> list[Document]:
    """Find documents that everyone has signed but which carry no completed copy.

    A copy is produced in the request that carries the last signature. Anything that interrupts
    that - a worker restart, storage refusing a write, or a fault in the sanitiser that was later
    corrected - leaves the signatures stored and the document short of the copy they were for,
    with nothing to notice. Voided documents are left alone: they were stopped deliberately.
    """
    candidates = (
        Document.objects.exclude(status=Document.StatusEnum.VOIDED)
        .filter(signing_requests__isnull=False)
        .exclude(status=Document.StatusEnum.COMPLETED, signed_pdf__gt="")
        .prefetch_related("signing_requests")
        .distinct()
    )
    return [document for document in candidates if every_signature_is_in(document) and not document.signed_pdf]
