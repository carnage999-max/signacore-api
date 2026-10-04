"""Three shapes a real survey used that detection did not read.

Found by sending a four-page employee survey through production, where it produced a field for
the rule under every page header, nothing at all for its table, and the word "Signature" against
the date line rather than the line people sign on.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from services.pdf_engine import PDFEngine

from . import pdf_builders as builders


class FormShapeDetectionTests(SimpleTestCase):
    def detect(self, data: bytes):
        handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        handle.write(data)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return PDFEngine().analyse(handle.name).fields

    def test_a_table_named_by_its_columns_offers_its_empty_cells(self) -> None:
        """Every row was either wholly full or wholly empty, so none qualified and none was read."""
        fields = self.detect(builders.build_column_headed_table_pdf())

        labels = [field.label for field in fields]
        self.assertIn("Project", labels)
        self.assertIn("How is it going?", labels)
        self.assertIn("Anything blocked?", labels)
        # Three columns across the three rows left empty beneath the headings.
        self.assertEqual(len([label for label in labels if label == "Project"]), 3)

    def test_the_heading_row_itself_is_not_offered_as_a_field(self) -> None:
        fields = self.detect(builders.build_column_headed_table_pdf())

        headings_row_top = max(field.y for field in fields if field.label == "Project")
        self.assertEqual(
            len([field for field in fields if field.y > headings_row_top]),
            0,
            "nothing should be asked for above the first empty row",
        )

    def test_a_caption_under_a_rule_names_that_rule(self) -> None:
        """Captions were only sought above or to the left, so a rule took its neighbour's."""
        fields = self.detect(builders.build_captions_under_rules_pdf())

        by_x = {round(field.x): field for field in sorted(fields, key=lambda field: field.x)}
        self.assertEqual(by_x[72].label, "Signature")
        self.assertEqual(by_x[330].label, "Date")
        self.assertEqual(by_x[460].label, "Initials")

    def test_the_line_people_sign_on_is_a_signature_field(self) -> None:
        """It was a plain text field named "Field 27", while the date line was the signature."""
        fields = self.detect(builders.build_captions_under_rules_pdf())

        signature = min(fields, key=lambda field: field.x)
        self.assertEqual(signature.field_type, "SIGNATURE")

    def test_a_rule_repeated_on_every_page_is_furniture(self) -> None:
        """A running header is the same shape as a writing line; what tells them apart is that it
        appears in the same place on every page."""
        fields = self.detect(builders.build_running_header_pdf(pages=4))

        header_rules = [field for field in fields if field.y > 740]
        self.assertEqual(header_rules, [], "the rule under the page header is not a question")

    def test_the_questions_on_those_pages_survive(self) -> None:
        fields = self.detect(builders.build_running_header_pdf(pages=4))

        self.assertEqual(len(fields), 4, "one writing line per page, and only that")

    def test_a_short_document_keeps_everything(self) -> None:
        """Two pages are not enough to tell a repeat from a coincidence."""
        fields = self.detect(builders.build_running_header_pdf(pages=2))

        self.assertTrue(any(field.y > 740 for field in fields))
