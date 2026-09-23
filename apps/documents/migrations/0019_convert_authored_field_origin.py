"""Convert authored document fields stored with a top-left origin.

The authored renderer measured its cursor from the top of the page and persisted that value
directly, while the signer portal and the flattener read ``y`` as a bottom-left coordinate. Every
field in an authored document therefore rendered mirrored down the page, the same defect that
``0017`` corrected for natively imported widgets.

Documents whose PDF cannot be read are left untouched and unmarked so a later run can still
repair them.
"""

from __future__ import annotations

import logging

import fitz
from django.db import migrations

from utils.file_storage import encrypted_file_storage

logger = logging.getLogger(__name__)

AUTHORED = "AUTHORED"
AUTHORED_ORIGIN_MARKER = "authored_origin_migrated"


def _page_heights(document) -> dict[int, float] | None:
    name = document.original_pdf.name if document.original_pdf else ""
    if not name:
        return None
    try:
        with encrypted_file_storage.open(name) as stored_file:
            payload = stored_file.read()
        with fitz.open(stream=payload, filetype="pdf") as pdf_document:
            return {number: float(page.rect.height) for number, page in enumerate(pdf_document, start=1)}
    except Exception:
        logger.warning("Skipping authored coordinate migration for document %s: PDF unreadable", document.pk)
        return None


def _flip(document_model, field_model, *, forward: bool) -> None:
    """Rewrite ``y`` between the two origins. The transform is its own inverse."""
    for document in document_model.objects.filter(fields__detection_source=AUTHORED).distinct().iterator():
        report = document.import_report or {}
        already_migrated = bool(report.get(AUTHORED_ORIGIN_MARKER))
        if forward and already_migrated:
            continue
        if not forward and not already_migrated:
            continue

        page_heights = _page_heights(document)
        if not page_heights:
            continue

        converted = []
        for field in field_model.objects.filter(document_id=document.pk, detection_source=AUTHORED):
            page_height = page_heights.get(field.page)
            if page_height is None:
                continue
            field.y = page_height - field.y - field.height
            converted.append(field)

        if converted:
            field_model.objects.bulk_update(converted, ["y"])

        report = dict(report)
        if forward:
            report[AUTHORED_ORIGIN_MARKER] = True
        else:
            report.pop(AUTHORED_ORIGIN_MARKER, None)
        document.import_report = report
        document.save(update_fields=["import_report"])


def convert_to_bottom_left_origin(apps, schema_editor) -> None:
    _flip(apps.get_model("documents", "Document"), apps.get_model("documents", "DocumentField"), forward=True)


def restore_top_left_origin(apps, schema_editor) -> None:
    _flip(apps.get_model("documents", "Document"), apps.get_model("documents", "DocumentField"), forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0018_documentfield_is_comb_documentfield_max_length_and_more"),
    ]

    operations = [
        migrations.RunPython(convert_to_bottom_left_origin, restore_top_left_origin),
    ]
