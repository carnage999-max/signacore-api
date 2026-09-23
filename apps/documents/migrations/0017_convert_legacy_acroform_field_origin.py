"""Convert natively imported fields stored with a top-left origin.

Native AcroForm widgets used to be persisted with PyMuPDF's top-left ``rect.y0``, while the
signer portal and the flattener both read ``y`` as a bottom-left coordinate. Those documents
render every imported field mirrored down the page, so their stored positions are rewritten
here to the model's bottom-left convention.

Only documents that predate the import report are touched: anything imported after the fix
already carries a report and is stored correctly. Documents whose PDF cannot be read are left
untouched and unmarked so a later run can still repair them.
"""

from __future__ import annotations

import logging

import fitz
from django.db import migrations

from utils.file_storage import encrypted_file_storage

logger = logging.getLogger(__name__)

LEGACY_ORIGIN_MARKER = "coordinate_origin_migrated"
ACROFORM = "ACROFORM"


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
        logger.warning("Skipping coordinate migration for document %s: PDF unreadable", document.pk)
        return None


def _flip(document_model, field_model, *, forward: bool) -> None:
    """Rewrite ``y`` between the two origins. The transform is its own inverse."""
    candidates = document_model.objects.filter(fields__detection_source=ACROFORM).distinct()

    for document in candidates.iterator():
        report = document.import_report or {}
        already_migrated = bool(report.get(LEGACY_ORIGIN_MARKER))
        if forward and report:
            continue
        if not forward and not already_migrated:
            continue

        page_heights = _page_heights(document)
        if not page_heights:
            continue

        fields = list(field_model.objects.filter(document_id=document.pk, detection_source=ACROFORM))
        converted = []
        for field in fields:
            page_height = page_heights.get(field.page)
            if page_height is None:
                continue
            field.y = page_height - field.y - field.height
            converted.append(field)

        if converted:
            field_model.objects.bulk_update(converted, ["y"])

        if forward:
            document.import_report = {
                "source": ACROFORM,
                "native_widget_count": len(fields),
                "imported_field_count": len(fields),
                "ignored_widget_count": 0,
                "warning_codes": [],
                LEGACY_ORIGIN_MARKER: True,
            }
        else:
            document.import_report = {}
        document.save(update_fields=["import_report"])


def convert_to_bottom_left_origin(apps, schema_editor) -> None:
    _flip(apps.get_model("documents", "Document"), apps.get_model("documents", "DocumentField"), forward=True)


def restore_top_left_origin(apps, schema_editor) -> None:
    _flip(apps.get_model("documents", "Document"), apps.get_model("documents", "DocumentField"), forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0016_document_import_report_and_more"),
    ]

    operations = [
        migrations.RunPython(convert_to_bottom_left_origin, restore_top_left_origin),
    ]
