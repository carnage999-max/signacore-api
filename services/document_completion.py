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


def issue_signed_copy(signing_request) -> None:
    """Flatten what this signer entered onto the document, and keep it as their own copy.

    One copy per signer, made the moment they finish, rather than one copy per document made when
    the last of them does. A document sent to a group to fill in separately is the ordinary case -
    a survey, a form, an acknowledgement - and the shared copy was never right for it: every
    signer's answers were drawn onto one page, so the result was unreadable and each of them
    received everybody else's.

    It also means a signer is not kept waiting on people they have never met. Their copy exists
    when they sign, because it is made of what they themselves put in.
    """
    from apps.signing.models import FieldSubmission

    submissions = list(
        FieldSubmission.objects.select_related("document_field")
        .filter(signing_request=signing_request)
        .order_by("submitted_at")
    )
    document = signing_request.document

    with ExitStack() as stack:
        source_path = stack.enter_context(temporary_plaintext_file(document.original_pdf, suffix=".pdf"))
        output_path = stack.enter_context(temporary_output_file(suffix=".pdf"))
        PDFEngine().flatten(
            source_path,
            output_path,
            _prefilled_payload(document) + _flatten_payload(submissions, stack),
        )
        save_encrypted_field_file(
            signing_request.signed_pdf,
            output_path,
            filename=f"{signing_request.id}-signed.pdf",
        )

    signing_request.save(update_fields=["signed_pdf", "updated_at"])


def refresh_document_status(document: Document) -> None:
    """Set where the document stands from where its signers stand.

    Worked out in one place because two callers need it: the request carrying a signature, and the
    sweep that makes a copy which that request failed to. Left to the request alone, a document
    whose last copy was made by the sweep would stay part-signed for ever.
    """
    from apps.signing.models import SigningRequest

    requests = document.signing_requests.all()
    if not requests:
        return

    outstanding = any(request.status != SigningRequest.StatusEnum.SIGNED for request in requests)
    status = Document.StatusEnum.PARTIALLY_SIGNED if outstanding else Document.StatusEnum.COMPLETED
    if document.status != status:
        document.status = status
        document.save(update_fields=["status", "updated_at"])


def _prefilled_payload(document) -> list[dict]:
    """What the sender filled in, drawn onto the copy the same way a signer's answers are.

    These carry no FieldSubmission - nobody submitted them, the sender set them on the document
    before it went out - so they would otherwise be absent from every completed copy and the
    agreement would arrive with its own terms missing.
    """
    from apps.documents.models import DocumentField

    payload = []
    for field in document.fields.all():
        if not field.is_prefilled:
            continue
        is_checkbox = field.field_type in {
            DocumentField.FieldTypeEnum.CHECKBOX,
            DocumentField.FieldTypeEnum.RADIO,
        }
        payload.append(
            {
                "page": field.page,
                "field_type": field.field_type,
                "max_length": field.max_length,
                "is_comb": field.is_comb,
                "x": field.x,
                "y": field.y,
                "width": field.width,
                "height": field.height,
                "value_type": "CHECKBOX" if is_checkbox else "TEXT",
                "text_value": field.prefilled_value,
                "image_path": "",
            }
        )
    return payload


def _flatten_payload(submissions, stack) -> list[dict]:
    """What the engine needs to draw, read once so both callers describe a field the same way."""
    payload = []
    for submission in submissions:
        image_path = ""
        if submission.image_value:
            image_path = str(stack.enter_context(temporary_plaintext_file(submission.image_value, suffix=".png")))
        payload.append(
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
    return payload


def signers_awaiting_their_copy() -> list:
    """Signers who finished but whose copy was never made.

    Flattening happens in the request that carries the signature. A worker restart, storage
    refusing a write, or a fault in the sanitiser later corrected leaves the signature stored and
    the copy missing, with nothing to notice.
    """
    from apps.signing.models import SigningRequest

    return list(
        SigningRequest.objects.select_related("document")
        .filter(status=SigningRequest.StatusEnum.SIGNED, signed_pdf="")
        .exclude(document__status=Document.StatusEnum.VOIDED)
    )


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

        PDFEngine().flatten(source_path, output_path, _prefilled_payload(document) + flatten_submissions)
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
