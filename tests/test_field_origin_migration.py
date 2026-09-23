from __future__ import annotations

import tempfile
from importlib import import_module

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.accounts.models import Organization
from apps.documents.models import Document, DocumentField

from . import pdf_builders as builders

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-origin-migration-")

migration = import_module("apps.documents.migrations.0017_convert_legacy_acroform_field_origin")

PAGE_HEIGHT = 792.0
LEGACY_TOP_Y = 118.0
FIELD_HEIGHT = 14.0
EXPECTED_BOTTOM_Y = PAGE_HEIGHT - LEGACY_TOP_Y - FIELD_HEIGHT


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class LegacyFieldOriginMigrationTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="admin", password="password123")
        self.organization = Organization.objects.create(name="Migration Co", created_by=self.user)

    def create_document(self, *, import_report: dict) -> Document:
        return Document.objects.create(
            title="Legacy import",
            original_pdf=SimpleUploadedFile(
                "legacy.pdf",
                builders.build_supported_acroform_pdf(),
                content_type="application/pdf",
            ),
            created_by=self.user,
            organization=self.organization,
            import_report=import_report,
        )

    def add_field(
        self,
        document: Document,
        *,
        detection_source: str = DocumentField.DetectionSourceEnum.ACROFORM,
        y: float = LEGACY_TOP_Y,
    ) -> DocumentField:
        return DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Name of entity",
            page=1,
            x=58.6,
            y=y,
            width=517.4,
            height=FIELD_HEIGHT,
            is_required=False,
            detection_source=detection_source,
            order=1,
        )

    def run_forward(self) -> None:
        migration._flip(Document, DocumentField, forward=True)

    def run_backward(self) -> None:
        migration._flip(Document, DocumentField, forward=False)

    def test_legacy_acroform_field_is_converted_to_bottom_left_origin(self) -> None:
        document = self.create_document(import_report={})
        field = self.add_field(document)

        self.run_forward()

        field.refresh_from_db()
        document.refresh_from_db()
        self.assertAlmostEqual(field.y, EXPECTED_BOTTOM_Y, places=3)
        self.assertTrue(document.import_report[migration.LEGACY_ORIGIN_MARKER])

    def test_conversion_is_not_applied_twice(self) -> None:
        document = self.create_document(import_report={})
        field = self.add_field(document)

        self.run_forward()
        self.run_forward()

        field.refresh_from_db()
        self.assertAlmostEqual(field.y, EXPECTED_BOTTOM_Y, places=3)

    def test_conversion_is_reversible(self) -> None:
        document = self.create_document(import_report={})
        field = self.add_field(document)

        self.run_forward()
        self.run_backward()

        field.refresh_from_db()
        document.refresh_from_db()
        self.assertAlmostEqual(field.y, LEGACY_TOP_Y, places=3)
        self.assertEqual(document.import_report, {})

    def test_documents_imported_after_the_fix_are_left_alone(self) -> None:
        document = self.create_document(
            import_report={
                "source": "ACROFORM",
                "native_widget_count": 3,
                "imported_field_count": 3,
                "ignored_widget_count": 0,
                "warning_codes": [],
            }
        )
        field = self.add_field(document, y=EXPECTED_BOTTOM_Y)

        self.run_forward()

        field.refresh_from_db()
        self.assertAlmostEqual(field.y, EXPECTED_BOTTOM_Y, places=3)

    def test_heuristic_and_manual_fields_are_never_touched(self) -> None:
        document = self.create_document(import_report={})
        acroform_field = self.add_field(document)
        heuristic_field = DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Heuristic field",
            page=1,
            x=72,
            y=300.0,
            width=180,
            height=15,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
            order=2,
        )
        manual_field = DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.SIGNATURE,
            label="Manual field",
            page=1,
            x=72,
            y=200.0,
            width=180,
            height=36,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=3,
        )

        self.run_forward()

        acroform_field.refresh_from_db()
        heuristic_field.refresh_from_db()
        manual_field.refresh_from_db()
        self.assertAlmostEqual(acroform_field.y, EXPECTED_BOTTOM_Y, places=3)
        self.assertAlmostEqual(heuristic_field.y, 300.0, places=3)
        self.assertAlmostEqual(manual_field.y, 200.0, places=3)

    def test_document_with_unreadable_pdf_is_skipped_and_left_unmarked(self) -> None:
        document = self.create_document(import_report={})
        field = self.add_field(document)
        document.original_pdf.storage.delete(document.original_pdf.name)

        self.run_forward()

        field.refresh_from_db()
        document.refresh_from_db()
        self.assertAlmostEqual(field.y, LEGACY_TOP_Y, places=3)
        self.assertEqual(document.import_report, {})
