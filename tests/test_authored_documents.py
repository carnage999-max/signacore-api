from __future__ import annotations

import tempfile
from importlib import import_module

import fitz
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import Organization
from apps.documents.models import Document, DocumentField
from services.authored_pdf import AuthoredPDFRenderer

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-authored-")

migration = import_module("apps.documents.migrations.0019_convert_authored_field_origin")


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def heading(text: str, level: int = 1) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]}


def signature_field(label: str = "Client signature") -> dict:
    return {"type": "signacoreField", "attrs": {"fieldType": "SIGNATURE", "label": label, "required": True}}


def build_content(*nodes: dict, page_size: str = "LETTER") -> dict:
    return {"type": "doc", "attrs": {"pageSize": page_size}, "content": list(nodes)}


class AuthoredRenderingTests(SimpleTestCase):
    """The renderer used to discard any block that did not fit, emptying whole documents."""

    def render(self, content: dict) -> tuple[fitz.Document, list]:
        pdf_bytes, fields = AuthoredPDFRenderer().render(content)
        return fitz.open("pdf", pdf_bytes), fields

    def test_paragraph_text_reaches_the_page(self) -> None:
        document, _ = self.render(build_content(paragraph("The parties agree to the attached terms.")))

        try:
            self.assertIn("The parties agree to the attached terms.", document[0].get_text())
        finally:
            document.close()

    def test_heading_and_body_are_both_rendered(self) -> None:
        document, _ = self.render(build_content(heading("Consulting Agreement"), paragraph("Effective today.")))

        try:
            text = document[0].get_text()
            self.assertIn("Consulting Agreement", text)
            self.assertIn("Effective today.", text)
        finally:
            document.close()

    def test_list_items_are_rendered(self) -> None:
        content = build_content(
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [paragraph("Deliver the statement of work")]},
                    {"type": "listItem", "content": [paragraph("Report progress weekly")]},
                ],
            }
        )

        document, _ = self.render(content)

        try:
            text = document[0].get_text()
            self.assertIn("Deliver the statement of work", text)
            self.assertIn("Report progress weekly", text)
        finally:
            document.close()

    def test_a_block_longer_than_one_page_continues_onto_the_next(self) -> None:
        sentence = "This clause restates the obligations of each party in full. "
        document, _ = self.render(build_content(paragraph(sentence * 120)))

        try:
            self.assertGreater(document.page_count, 1)
            for page in document:
                self.assertTrue(page.get_text().strip(), "every page of a split block must carry text")
        finally:
            document.close()

    def test_field_position_is_stored_in_the_bottom_left_origin(self) -> None:
        document, fields = self.render(build_content(heading("Agreement"), signature_field()))

        try:
            page_height = document[0].rect.height
            field = fields[0]
            # The signer portal and the flattener both derive the top edge this way.
            rendered_top = page_height - field.y - field.height
            self.assertGreater(rendered_top, 0)
            self.assertLess(rendered_top, page_height)

            rules = [
                drawing
                for drawing in document[0].get_drawings()
                if abs(drawing["rect"].y1 - (rendered_top + field.height)) < 6
            ]
            self.assertTrue(rules, "the drawn signature rule must sit where the stored field resolves to")
        finally:
            document.close()


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class AuthoredFieldOriginMigrationTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="author", password="password123")
        self.organization = Organization.objects.create(name="Authoring Co", created_by=self.user)
        pdf_bytes, _ = AuthoredPDFRenderer().render(build_content(heading("Agreement"), signature_field()))
        self.document = Document.objects.create(
            title="Authored agreement",
            source=Document.SourceEnum.AUTHORED,
            original_pdf=SimpleUploadedFile("authored.pdf", pdf_bytes, content_type="application/pdf"),
            created_by=self.user,
            organization=self.organization,
        )
        self.field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.SIGNATURE,
            label="Client signature",
            page=1,
            x=54.0,
            y=120.0,
            width=190.0,
            height=38.0,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.AUTHORED,
            order=1,
        )

    def run_forward(self) -> None:
        migration._flip(Document, DocumentField, forward=True)

    def test_authored_field_is_converted_to_bottom_left_origin(self) -> None:
        self.run_forward()

        self.field.refresh_from_db()
        self.document.refresh_from_db()
        self.assertAlmostEqual(self.field.y, 792.0 - 120.0 - 38.0, places=3)
        self.assertTrue(self.document.import_report[migration.AUTHORED_ORIGIN_MARKER])

    def test_conversion_is_not_applied_twice(self) -> None:
        self.run_forward()
        self.run_forward()

        self.field.refresh_from_db()
        self.assertAlmostEqual(self.field.y, 792.0 - 120.0 - 38.0, places=3)

    def test_conversion_is_reversible(self) -> None:
        self.run_forward()
        migration._flip(Document, DocumentField, forward=False)

        self.field.refresh_from_db()
        self.document.refresh_from_db()
        self.assertAlmostEqual(self.field.y, 120.0, places=3)
        self.assertNotIn(migration.AUTHORED_ORIGIN_MARKER, self.document.import_report)

    def test_imported_fields_are_never_touched(self) -> None:
        imported = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Imported field",
            page=1,
            x=72.0,
            y=300.0,
            width=180.0,
            height=24.0,
            is_required=False,
            detection_source=DocumentField.DetectionSourceEnum.ACROFORM,
            order=2,
        )

        self.run_forward()

        imported.refresh_from_db()
        self.assertAlmostEqual(imported.y, 300.0, places=3)
