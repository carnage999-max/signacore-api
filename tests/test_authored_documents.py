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


def paragraph(text: str, *, align: str = "") -> dict:
    node: dict = {"type": "paragraph", "content": [{"type": "text", "text": text}]}
    if align:
        node["attrs"] = {"textAlign": align}
    return node


def marked_paragraph(*runs: tuple[str, str]) -> dict:
    content = []
    for text, mark in runs:
        node: dict = {"type": "text", "text": text}
        if mark:
            node["marks"] = [{"type": mark}]
        content.append(node)
    return {"type": "paragraph", "content": content}


def table_cell(text: str, *, header: bool = False) -> dict:
    return {"type": "tableHeader" if header else "tableCell", "content": [paragraph(text)]}


def table(rows: list[list[dict]]) -> dict:
    return {"type": "table", "content": [{"type": "tableRow", "content": row} for row in rows]}


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


class AuthoredFormattingTests(SimpleTestCase):
    """Formatting the editor offers must survive into the rendered document."""

    def render(self, content: dict) -> fitz.Document:
        pdf_bytes, _ = AuthoredPDFRenderer().render(content)
        return fitz.open("pdf", pdf_bytes)

    def test_bold_and_italic_runs_keep_their_text(self) -> None:
        content = build_content(
            marked_paragraph(("This agreement binds ", ""), ("ACME Holdings", "bold"), (" today.", "italic"))
        )

        document = self.render(content)
        try:
            self.assertIn("ACME Holdings", document[0].get_text())
            self.assertIn("This agreement binds", document[0].get_text())
        finally:
            document.close()

    def test_bold_text_is_drawn_in_a_bold_face(self) -> None:
        content = build_content(marked_paragraph(("Plain ", ""), ("Emphasised", "bold")))

        document = self.render(content)
        try:
            fonts = {
                span["font"]
                for block in document[0].get_text("dict")["blocks"]
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if "Emphasised" in span["text"]
            }
        finally:
            document.close()

        self.assertTrue(fonts)
        self.assertTrue(any("Bold" in font for font in fonts), fonts)

    def test_table_cells_are_rendered(self) -> None:
        content = build_content(
            table(
                [
                    [table_cell("Milestone", header=True), table_cell("Amount", header=True)],
                    [table_cell("Kick-off"), table_cell("$5,000")],
                ]
            )
        )

        document = self.render(content)
        try:
            text = document[0].get_text()
            for expected in ("Milestone", "Amount", "Kick-off", "$5,000"):
                self.assertIn(expected, text)
        finally:
            document.close()

    def test_table_is_drawn_with_grid_lines(self) -> None:
        content = build_content(table([[table_cell("A"), table_cell("B")]]))

        document = self.render(content)
        try:
            self.assertTrue(document[0].get_drawings(), "a table must draw its grid")
        finally:
            document.close()

    def test_a_table_is_accepted_by_validation(self) -> None:
        content = build_content(table([[table_cell("A"), table_cell("B")]]))

        self.assertEqual(AuthoredPDFRenderer().validate(content), content)

    def test_a_table_row_beyond_the_column_limit_is_rejected(self) -> None:
        wide_row = [table_cell(str(index)) for index in range(AuthoredPDFRenderer.max_table_columns + 1)]

        with self.assertRaises(ValueError):
            AuthoredPDFRenderer().validate(build_content(table([wide_row])))

    def test_centred_text_is_not_placed_at_the_left_margin(self) -> None:
        centred = self.render(build_content(paragraph("Consulting Agreement", align="center")))
        left = self.render(build_content(paragraph("Consulting Agreement")))
        try:
            centred_x = centred[0].get_text("words")[0][0]
            left_x = left[0].get_text("words")[0][0]
        finally:
            centred.close()
            left.close()

        self.assertGreater(centred_x, left_x + 20)

    def test_right_aligned_text_sits_further_right_than_centred_text(self) -> None:
        right = self.render(build_content(paragraph("Countersigned", align="right")))
        centred = self.render(build_content(paragraph("Countersigned", align="center")))
        try:
            right_x = right[0].get_text("words")[0][0]
            centred_x = centred[0].get_text("words")[0][0]
        finally:
            right.close()
            centred.close()

        self.assertGreater(right_x, centred_x)

    def test_text_content_cannot_inject_layout_markup(self) -> None:
        content = build_content(paragraph("Pay <b>nothing</b> under clause 4"))

        document = self.render(content)
        try:
            self.assertIn("<b>nothing</b>", document[0].get_text())
        finally:
            document.close()

    def test_a_field_after_a_long_body_is_reported_on_the_page_it_landed_on(self) -> None:
        sentence = "This clause restates the obligations of each party in full. "
        content = build_content(paragraph(sentence * 120), signature_field())

        pdf_bytes, fields = AuthoredPDFRenderer().render(content)
        document = fitz.open("pdf", pdf_bytes)
        try:
            self.assertGreater(document.page_count, 1)
            self.assertEqual(len(fields), 1)
            self.assertGreaterEqual(fields[0].page, 1)
            self.assertLessEqual(fields[0].page, document.page_count)

            page = document[fields[0].page - 1]
            rules = [
                drawing
                for drawing in page.get_drawings()
                if abs(drawing["rect"].y1 - (page.rect.height - fields[0].y)) < 6
            ]
            self.assertTrue(rules, "the field rule must be drawn on the page the field reports")
        finally:
            document.close()
