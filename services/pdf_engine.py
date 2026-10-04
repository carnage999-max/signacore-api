from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from apps.documents.models import DocumentField
from services.pdf_anchors import anchor_field_rect, conceal_anchor_tags, find_anchor_tags
from services.pdf_form_import import (
    ImportReport,
    ImportWarningEnum,
    classify_widget,
    collect_text_lines,
    collect_widget_scripts,
    is_comb_widget,
    is_widget_required,
    radio_group_key,
    resolve_label,
    shared_tooltip_labels,
    widget_max_length,
    widget_on_state,
    widget_options,
)
from services.pdf_sanitizer import assert_sanitized, sanitize_document

MIN_TEXT_LAYER_CHARS = 24
# A running header or footer is ruled off across the full width of the page. Nobody signs in the
# margin, so a rule that sits in one is decoration rather than a place to write.
HEADER_FOOTER_MARGIN = 48.0
# How far below a caption its writing rule is drawn, when the caption stands beside it.
UNDERLINE_GAP = 10.0
# How far below a caption written over the rule that rule may sit. A caption on its own line
# clears the descenders of its own text before the rule begins, so it sits further off.
ABOVE_CAPTION_GAP = 18.0
# How far to the left of a rule a caption written above it may end. A caption beside a rule is not
# held to this, because a form's label column can sit well clear of the space it labels.
BESIDE_CAPTION_BAND = 14.0
# A caption written under its rule, as a signature block does, and starting where the rule starts.
BELOW_CAPTION_GAP = 10.0
BELOW_CAPTION_ALIGNMENT = 12.0
# How far down a running header can sit. Only applied to something that also repeats on every
# page, so it does not have to be tight enough to identify a header on its own.
RUNNING_HEADER_BAND = 120.0
# A space wider than this between two words of a row separates one column from the next rather
# than one word of a caption from the next.
CAPTION_WORD_GAP = 26.0
# Rules within this distance of each other are the same line of a grid drawn cell by cell.
GRID_LINE_TOLERANCE = 2.0
# A cell smaller than this is a rule or a tick box rather than somewhere to write, and one taller
# than this is a panel holding its own layout rather than a row of a form.
MIN_CELL_WIDTH = 40.0
MIN_CELL_HEIGHT = 8.0
MAX_CELL_HEIGHT = 90.0
# Keeps the field inside the printed cell rather than sitting on its border.
CELL_PADDING = 1.5
# The least room a written answer is given, whatever the text beside the line happens to measure.
MIN_WRITING_HEIGHT = 18.0
# Ruled lines this far apart or less belong to the same written answer, and this many of them make
# a block rather than a coincidence.
MAX_ANSWER_LINE_PITCH = 40.0
MIN_ANSWER_BLOCK_LINES = 3
# Words that join a clause to the rest of its sentence. A field caption never hangs on one.
CLAUSE_WORDS = frozenset(
    """a an and as at because before but by for from if in of on or since than that the
    to unless until when which while with without""".split()
)


@dataclass
class DetectedField:
    field_type: str
    label: str
    page: int
    x: float
    y: float
    width: float
    height: float
    is_required: bool
    detection_source: str
    order: int
    max_length: int | None = None
    is_comb: bool = False
    options: list[str] | None = None
    group_key: str = ""
    option_value: str = ""
    # False when nothing on the page named this field and it carries a positional placeholder.
    is_named: bool = True


@dataclass
class ImportResult:
    fields: list[DetectedField]
    report: ImportReport


class PDFEngine:
    signature_keywords = ("signature",)
    initials_keywords = ("initials", "int.")
    text_keywords = ("name", "date", "email", "print", "tenant", "landlord", "rent", "term", "unit")
    max_label_length = 255
    checkbox_chars = ("☐", "□")
    underscore_pattern = re.compile(r"_{3,}")
    flatten_fontname = "helv"
    max_flatten_font_size = 12.0
    min_flatten_font_size = 4.5
    text_left_padding = 1.5
    text_cap_height_ratio = 0.72

    def analyse(self, pdf_path: str | Path) -> ImportResult:
        document = fitz.open(pdf_path)
        try:
            anchor_fields, unplaced_tags = self._import_anchor_tags(document)
            if anchor_fields:
                report = ImportReport(
                    source=DocumentField.DetectionSourceEnum.ANCHOR,
                    imported_field_count=len(anchor_fields),
                )
                if unplaced_tags:
                    report.warning_codes.append(ImportWarningEnum.ANCHOR_TAG_NOT_PLACED)
                return ImportResult(fields=anchor_fields, report=report)

            native_result = self._import_native_widgets(document)
            if native_result.report.native_widget_count:
                return self._add_fields_from_plain_pages(document, native_result)

            heuristic_fields = self.detect_heuristic(document)
            report = ImportReport(
                source=DocumentField.DetectionSourceEnum.HEURISTIC,
                imported_field_count=len(heuristic_fields),
            )
            if unplaced_tags:
                report.warning_codes.append(ImportWarningEnum.ANCHOR_TAG_NOT_PLACED)
            if not heuristic_fields:
                report.warning_codes.append(
                    ImportWarningEnum.SCANNED_DOCUMENT_NO_TEXT_LAYER
                    if self._looks_scanned(document)
                    else ImportWarningEnum.NO_FIELDS_DETECTED
                )
            return ImportResult(fields=heuristic_fields, report=report)
        finally:
            document.close()

    @staticmethod
    def _looks_scanned(document: fitz.Document) -> bool:
        """A page-sized image with no extractable text is a scan, not a form we can read.

        Heuristic detection needs a text layer or vector rules, so these documents yield
        nothing and the administrator has to be told why rather than shown an empty result.
        """
        has_text = False
        has_images = False
        for page in document:
            if len(page.get_text("text").strip()) >= MIN_TEXT_LAYER_CHARS:
                has_text = True
                break
            if page.get_images(full=True):
                has_images = True
        return has_images and not has_text

    def _import_anchor_tags(self, document: fitz.Document) -> tuple[list[DetectedField], int]:
        """Turn each anchor tag into a field at the tag's own position.

        Anchor tags are explicit author intent, so they take precedence over native widgets and
        suppress heuristics entirely.
        """
        scan = find_anchor_tags(document)
        detected_fields: list[DetectedField] = []
        for order, match in enumerate(scan.matches, start=1):
            rect = anchor_field_rect(match)
            page_height = document[match.page - 1].rect.height
            detected_fields.append(
                DetectedField(
                    field_type=match.field_type,
                    label=self._normalize_label(match.label),
                    page=match.page,
                    x=rect.x0,
                    y=page_height - rect.y1,
                    width=rect.width,
                    height=rect.height,
                    is_required=match.is_required,
                    detection_source=DocumentField.DetectionSourceEnum.ANCHOR,
                    order=order,
                )
            )
        return detected_fields, scan.unplaced_tag_count

    def _add_fields_from_plain_pages(self, document: fitz.Document, native_result: ImportResult) -> ImportResult:
        """Run heuristics over the pages that carry no widgets of their own.

        Packets are assembled from unrelated sources, so a fillable government form can sit between
        an agreement and a policy that were only ever laid out to be printed. A page holding widgets
        is described entirely by them and is left alone, which keeps an unsupported control from
        being recreated from its visual outline. A page holding none has nothing to recreate, and
        skipping it would silently drop every signature line on it.
        """
        widget_pages = frozenset(index for index, page in enumerate(document, start=1) if list(page.widgets() or []))
        if len(widget_pages) == document.page_count:
            return native_result

        extra_fields = self.detect_heuristic(document, skip_pages=widget_pages)
        if not extra_fields:
            return native_result

        fields = native_result.fields + extra_fields
        for order, detected_field in enumerate(fields, start=1):
            detected_field.order = order

        report = native_result.report
        report.imported_field_count = len(fields)
        # SIGNATURE_FIELD_NOT_DETECTED is deliberately left alone. It reports that the form's own
        # pages declared nowhere to sign, which a signature line found on an unrelated page of the
        # same file does not answer - the W-9's instruction pages must not silence the prompt to
        # place a signature field on the form itself.
        report.warning_codes = [
            code for code in report.warning_codes if code != ImportWarningEnum.NO_SUPPORTED_FIELDS_IMPORTED
        ]
        return ImportResult(fields=fields, report=report)

    def _import_native_widgets(self, document: fitz.Document) -> ImportResult:
        """Import only widgets SignaCore can represent faithfully, reporting everything skipped.

        A page that has native widgets never falls back to heuristics, even when every widget on it
        is skipped, so unsupported controls are not recreated from their visual outlines.
        """
        detected_fields: list[DetectedField] = []
        report = ImportReport(source=DocumentField.DetectionSourceEnum.ACROFORM)
        order = 1

        for page_index, page in enumerate(document, start=1):
            widgets = list(page.widgets() or [])
            if not widgets:
                continue
            text_lines = collect_text_lines(page)
            page_tooltips = shared_tooltip_labels(widgets)
            page_height = page.rect.height
            page_field_index = 1
            label_counts: dict[str, int] = {}
            row_labels: list[tuple[float, str]] = []

            for widget in widgets:
                report.native_widget_count += 1
                label, is_confident = resolve_label(
                    widget, text_lines, page_index, page_field_index, shared_tooltips=page_tooltips
                )
                field_type, warning_code = classify_widget(document, widget, label)
                if warning_code:
                    report.warning_codes.append(warning_code)
                if field_type is None:
                    # A warning without a type means the widget was skipped; with a type it is
                    # advisory, and the field is still imported.
                    report.ignored_widget_count += 1
                    continue

                row_centre = (widget.rect.y0 + widget.rect.y1) / 2
                if not is_confident:
                    inherited = self._label_from_same_row(row_centre, row_labels)
                    if inherited:
                        label = inherited
                        is_confident = True
                if is_confident:
                    row_labels.append((row_centre, label))

                label_counts[label] = label_counts.get(label, 0) + 1
                if label_counts[label] > 1:
                    label = f"{label} ({label_counts[label]})"

                max_length = widget_max_length(document, widget)
                options = widget_options(widget) if field_type == DocumentField.FieldTypeEnum.DROPDOWN else None
                group_key = option_value = ""
                if field_type == DocumentField.FieldTypeEnum.RADIO:
                    group_key = radio_group_key(widget, collect_widget_scripts(document, widget))
                    option_value = widget_on_state(widget) or label
                rect = widget.rect
                detected_fields.append(
                    DetectedField(
                        max_length=max_length,
                        is_comb=is_comb_widget(document, widget, max_length),
                        options=options,
                        group_key=group_key,
                        option_value=option_value,
                        field_type=field_type,
                        label=self._normalize_label(label),
                        page=page_index,
                        x=rect.x0,
                        y=page_height - rect.y1,
                        width=rect.width,
                        height=rect.height,
                        is_required=is_widget_required(widget),
                        detection_source=DocumentField.DetectionSourceEnum.ACROFORM,
                        order=order,
                    )
                )
                order += 1
                page_field_index += 1

        report.imported_field_count = len(detected_fields)
        if report.native_widget_count:
            if not detected_fields:
                report.warning_codes.append(ImportWarningEnum.NO_SUPPORTED_FIELDS_IMPORTED)
            if not any(field.field_type == DocumentField.FieldTypeEnum.SIGNATURE for field in detected_fields):
                report.warning_codes.append(ImportWarningEnum.SIGNATURE_FIELD_NOT_DETECTED)
        return ImportResult(fields=detected_fields, report=report)

    @staticmethod
    def _label_from_same_row(row_centre: float, row_labels: list[tuple[float, str]]) -> str:
        """Reuse a neighbour's caption for a widget that has none of its own.

        Split fields - a comb SSN broken into area, group and serial boxes - share one printed
        caption, so the boxes after the first would otherwise fall back to a positional name.
        """
        for centre, label in reversed(row_labels):
            if abs(centre - row_centre) <= 6.0:
                return label
        return ""

    def detect_heuristic(
        self, document: fitz.Document, skip_pages: frozenset[int] = frozenset()
    ) -> list[DetectedField]:
        detected_fields: list[DetectedField] = []
        page_heights: dict[int, float] = {}
        order = 1
        for page_index, page in enumerate(document, start=1):
            if page_index in skip_pages:
                continue
            page_height = page.rect.height
            page_heights[page_index] = page_height
            line_words = self._collect_line_words(page)
            drawings = page.get_drawings()
            page_candidates: list[DetectedField] = []

            underscore_candidates = self._extract_underscore_fields(
                line_words=line_words,
                page_index=page_index,
                page_height=page_height,
                order_start=order,
            )
            page_candidates.extend(underscore_candidates)
            order += len(underscore_candidates)

            checkbox_candidates = self._extract_unicode_checkbox_fields(
                line_words=line_words,
                page_index=page_index,
                page_height=page_height,
                order_start=order,
            )
            page_candidates.extend(checkbox_candidates)
            order += len(checkbox_candidates)

            short_label_candidates = self._extract_short_label_fields(
                line_words=line_words,
                page_index=page_index,
                page_height=page_height,
                order_start=order,
            )
            page_candidates.extend(short_label_candidates)
            order += len(short_label_candidates)

            drawn_checkbox_candidates = self._extract_drawn_checkbox_fields(
                line_words=line_words,
                drawings=drawings,
                page_index=page_index,
                page_height=page_height,
                order_start=order,
            )
            page_candidates.extend(drawn_checkbox_candidates)
            order += len(drawn_checkbox_candidates)

            line_candidates = self._extract_labeled_horizontal_line_fields(
                line_words=line_words,
                drawings=drawings,
                page_index=page_index,
                page_height=page_height,
                order_start=order,
            )
            page_candidates.extend(line_candidates)
            order += len(line_candidates)

            cell_candidates = self._extract_table_cell_fields(
                line_words=line_words,
                drawings=drawings,
                page_index=page_index,
                page_height=page_height,
                order_start=order,
            )
            page_candidates.extend(cell_candidates)
            order += len(cell_candidates)

            deduped_candidates = self._settle_heuristic_fields(
                self._merge_written_answer_lines(self._deduplicate_fields(page_candidates))
            )
            for index, candidate in enumerate(deduped_candidates, start=order - len(page_candidates)):
                candidate.order = index
            detected_fields.extend(deduped_candidates)
            order = len(detected_fields) + 1
        return self._drop_page_furniture(self._renumber(detected_fields), len(document) - len(skip_pages), page_heights)

    @staticmethod
    def _drop_page_furniture(
        fields: list[DetectedField], page_count: int, page_heights: dict[int, float]
    ) -> list[DetectedField]:
        """Remove what the top of every page repeats rather than asks for.

        A running header is a rule across the width of the page with the document's title beside
        it, which is the same shape as a writing line with a caption. Two things together tell
        them apart, and neither is enough alone.

        Repetition: a question is asked once, while a header appears in the same place on every
        page. On its own that would also describe a contract asking for initials on each page,
        which is a real and important thing to ask for.

        Height: a header sits above the body. On its own that is only a distance from the edge,
        and a margin wide enough to catch this one - the rule was 76pt down - would swallow the
        first question of documents with a shallower top margin.

        Requiring both leaves a repeated question in the body alone, and an initials line at the
        foot of every page, while removing the furniture at the top.
        """
        if page_count < 3:
            return fields

        seen: dict[tuple[int, int, int], set[int]] = {}
        for field in fields:
            seen.setdefault((round(field.x), round(field.y), round(field.width)), set()).add(field.page)

        kept = []
        for field in fields:
            repeated = len(seen[(round(field.x), round(field.y), round(field.width))]) >= page_count
            from_top = page_heights.get(field.page, 0.0) - (field.y + field.height)
            if repeated and from_top <= RUNNING_HEADER_BAND:
                continue
            kept.append(field)
        return PDFEngine._renumber(kept)

    @staticmethod
    def _renumber(fields: list[DetectedField]) -> list[DetectedField]:
        for index, field in enumerate(fields, start=1):
            field.order = index
        return fields

    def flatten(self, source_pdf: str | Path, output_pdf: str | Path, submissions: list[dict[str, Any]]) -> None:
        document = fitz.open(source_pdf)
        try:
            # Tags are removed before values are drawn, because redaction clears whatever
            # already occupies the area it is clearing.
            conceal_anchor_tags(document)
            for submission in submissions:
                page = document[submission["page"] - 1]
                page_height = page.rect.height
                rect = fitz.Rect(
                    submission["x"],
                    page_height - submission["y"] - submission["height"],
                    submission["x"] + submission["width"],
                    page_height - submission["y"],
                )
                if submission["value_type"] == "TEXT":
                    if submission.get("field_type") == DocumentField.FieldTypeEnum.MULTILINE:
                        self._draw_wrapped_text(page, rect, submission["text_value"])
                    elif submission.get("is_comb") and submission.get("max_length"):
                        self._draw_comb_text(page, rect, submission["text_value"], int(submission["max_length"]))
                    else:
                        self._draw_single_line_text(page, rect, submission["text_value"])
                elif submission["value_type"] == "CHECKBOX":
                    if str(submission["text_value"]).lower() in {"true", "1", "yes", "on"}:
                        if submission.get("field_type") == DocumentField.FieldTypeEnum.RADIO:
                            self._draw_radio_mark(page, rect)
                        else:
                            self._draw_check_mark(page, rect)
                else:
                    page.insert_image(rect, filename=submission["image_path"])

            sanitize_document(document)
            document.save(output_pdf, garbage=4, clean=True, deflate=True)
        finally:
            document.close()
        assert_sanitized(output_pdf)

    @staticmethod
    def _field_text_font_size(rect: fitz.Rect) -> float:
        return max(7.0, min(12.0, rect.height * 0.72, rect.width * 0.16))

    @staticmethod
    def _multiline_text_font_size(rect: fitz.Rect) -> float:
        """Multiline boxes wrap, so size by line height rather than by the whole rectangle."""
        return max(7.0, min(11.0, rect.width * 0.16))

    def _draw_single_line_text(self, page: fitz.Page, rect: fitz.Rect, value: str) -> None:
        """Draw one line on its baseline.

        ``insert_textbox`` silently discards text whose line box does not fit, which blanks
        every realistically sized form field, so single-line values are placed directly.
        """
        text = str(value or "").strip()
        if not text:
            return
        font_size = self._fitted_font_size(text, rect)
        baseline = rect.y0 + (rect.height + font_size * self.text_cap_height_ratio) / 2
        page.insert_text(
            fitz.Point(rect.x0 + self.text_left_padding, baseline),
            text,
            fontname=self.flatten_fontname,
            fontsize=font_size,
        )

    def _draw_comb_text(self, page: fitz.Page, rect: fitz.Rect, value: str, max_length: int) -> None:
        """Place one character per comb cell so digits line up with the printed dividers."""
        text = str(value or "").strip()
        if not text or max_length < 1:
            return
        cell_width = rect.width / max_length
        font_size = min(self.max_flatten_font_size, rect.height * 0.62, cell_width * 1.35)
        font_size = max(self.min_flatten_font_size, font_size)
        baseline = rect.y0 + (rect.height + font_size * self.text_cap_height_ratio) / 2

        for index, character in enumerate(text[:max_length]):
            glyph_width = fitz.get_text_length(character, fontname=self.flatten_fontname, fontsize=font_size)
            centre = rect.x0 + (cell_width * index) + (cell_width / 2)
            page.insert_text(
                fitz.Point(centre - (glyph_width / 2), baseline),
                character,
                fontname=self.flatten_fontname,
                fontsize=font_size,
            )

    def _fitted_font_size(self, text: str, rect: fitz.Rect) -> float:
        usable_width = max(rect.width - (2 * self.text_left_padding), 1.0)
        font_size = min(self.max_flatten_font_size, rect.height * 0.82)
        while font_size > self.min_flatten_font_size:
            width = fitz.get_text_length(text, fontname=self.flatten_fontname, fontsize=font_size)
            if width <= usable_width:
                return font_size
            font_size -= 0.25
        return self.min_flatten_font_size

    def _draw_wrapped_text(self, page: fitz.Page, rect: fitz.Rect, value: str) -> None:
        """Wrap into the box, shrinking until the whole value fits rather than dropping it."""
        text = str(value or "").strip()
        if not text:
            return
        font_size = self._multiline_text_font_size(rect)
        while font_size > self.min_flatten_font_size:
            if page.insert_textbox(rect, text, fontname=self.flatten_fontname, fontsize=font_size) >= 0:
                return
            font_size -= 0.5
        page.insert_textbox(rect, text, fontname=self.flatten_fontname, fontsize=self.min_flatten_font_size)

    @staticmethod
    def _draw_radio_mark(page: fitz.Page, rect: fitz.Rect) -> None:
        """Fill the chosen option, the way a radio button is marked on paper."""
        radius = min(rect.width, rect.height) * 0.29
        centre = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        shape = page.new_shape()
        shape.draw_circle(centre, radius)
        shape.finish(color=(0, 0, 0), fill=(0, 0, 0))
        shape.commit()

    @staticmethod
    def _draw_check_mark(page: fitz.Page, rect: fitz.Rect) -> None:
        """Draw a vector tick, which scales to any box instead of needing a font to fit."""
        inset = min(rect.width, rect.height) * 0.2
        box = fitz.Rect(rect.x0 + inset, rect.y0 + inset, rect.x1 - inset, rect.y1 - inset)
        elbow = fitz.Point(box.x0 + box.width * 0.38, box.y1)
        shape = page.new_shape()
        shape.draw_line(fitz.Point(box.x0, box.y0 + box.height * 0.5), elbow)
        shape.draw_line(elbow, fitz.Point(box.x1, box.y0))
        shape.finish(color=(0, 0, 0), width=max(0.6, min(rect.width, rect.height) * 0.14))
        shape.commit()

    def _heuristic_type_for_text(self, text: str) -> str | None:
        normalized = str(text or "").lower()
        if any(keyword in normalized for keyword in self.initials_keywords):
            return DocumentField.FieldTypeEnum.INITIALS
        if re.search(r"\b(date|email|printed|print|print name|name)\b", normalized):
            return DocumentField.FieldTypeEnum.TEXT
        if any(keyword in normalized for keyword in self.signature_keywords):
            return DocumentField.FieldTypeEnum.SIGNATURE
        if any(keyword in normalized for keyword in self.text_keywords) or normalized.endswith(":"):
            return DocumentField.FieldTypeEnum.TEXT
        return None

    def _normalize_label(self, value: str) -> str:
        label = str(value or "").strip()
        if not label:
            return "Field"
        return label[: self.max_label_length]

    def _collect_line_words(self, page: fitz.Page) -> list[list[tuple[Any, ...]]]:
        grouped: dict[tuple[int, int], list[tuple[Any, ...]]] = {}
        for word in self._split_leading_checkboxes(page):
            grouped.setdefault((int(word[5]), int(word[6])), []).append(word)
        return [sorted(words, key=lambda item: (item[1], item[0])) for _, words in sorted(grouped.items())]

    def _split_leading_checkboxes(self, page: fitz.Page) -> list[tuple[Any, ...]]:
        """Separate a tick box from the label stuck to it.

        A word is whatever sits between two spaces, and a page laid out with CSS has no space
        between a box and the option beside it - the gap is margin, which leaves no character. The
        box therefore arrives as part of the first word of its label: not "☐" but "☐Less", which
        matches nothing looking for a box. A four-page survey carried seventy-four of them and
        offered five, those five being the only ones whose labels sat underneath rather than
        beside.

        The box is given back its own word, with the width the glyph actually occupies rather than
        a guess, so what follows reads the geometry of the box and not of the box and its label
        together.
        """
        boxes = self._checkbox_glyph_boxes(page)
        words: list[tuple[Any, ...]] = []
        for word in page.get_text("words"):
            token = str(word[4] or "")
            if len(token) < 2 or token[0] not in self.checkbox_chars:
                words.append(word)
                continue

            x0, y0, x1, y1 = map(float, word[:4])
            glyph = self._glyph_at(boxes, x0, y0, y1)
            # Without the glyph's own box there is no honest width for it, and inventing one would
            # put the field somewhere the box is not.
            if glyph is None:
                words.append(word)
                continue

            words.append((glyph.x0, glyph.y0, glyph.x1, glyph.y1, token[0], word[5], word[6], word[7]))
            words.append((glyph.x1, y0, x1, y1, token[1:], word[5], word[6], word[7]))
        return words

    def _checkbox_glyph_boxes(self, page: fitz.Page) -> list[fitz.Rect]:
        """Where each tick box sits, read a character at a time rather than a word at a time."""
        found: list[fitz.Rect] = []
        for block in page.get_text("rawdict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        if char.get("c") in self.checkbox_chars:
                            found.append(fitz.Rect(char["bbox"]))
        return found

    @staticmethod
    def _glyph_at(boxes: list[fitz.Rect], x0: float, y0: float, y1: float) -> fitz.Rect | None:
        """The box that opens this word.

        Matched by where it starts and by sharing the word's line rather than by an exact
        position: a character's box and the box of the word containing it are measured from
        different things and differ by a fraction of a point.
        """
        for rect in boxes:
            if abs(rect.x0 - x0) <= 1.0 and rect.y0 < y1 and rect.y1 > y0:
                return rect
        return None

    def _extract_underscore_fields(
        self,
        line_words: list[list[tuple[Any, ...]]],
        page_index: int,
        page_height: float,
        order_start: int,
    ) -> list[DetectedField]:
        fields: list[DetectedField] = []
        order = order_start
        for words in line_words:
            line_height = self._line_height_for_words(words)
            if line_height is None:
                continue
            line_text = " ".join(str(word[4]) for word in words).strip()
            if "_" not in line_text:
                continue

            for index, word in enumerate(words):
                token = str(word[4] or "")
                match = self.underscore_pattern.search(token)
                if not match:
                    continue

                x0, y0, x1, y1 = map(float, word[:4])
                prefix = token[: match.start()].strip(" :.-")
                suffix = token[match.end() :].strip(" :.-")
                char_width = (x1 - x0) / max(len(token), 1)
                field_x0 = x0 + (match.start() * char_width)
                field_x1 = x0 + (match.end() * char_width)
                if field_x1 - field_x0 < 18:
                    continue

                prior_words = [str(item[4]) for item in words[max(0, index - 4) : index]]
                next_words = [str(item[4]) for item in words[index + 1 : index + 4]]
                prefix_type = self._heuristic_type_for_text(prefix.lower()) if prefix else None
                if prefix and prefix_type in {
                    DocumentField.FieldTypeEnum.TEXT,
                    DocumentField.FieldTypeEnum.SIGNATURE,
                    DocumentField.FieldTypeEnum.INITIALS,
                }:
                    label_context = self._merge_prefix_with_prior_words(prefix, prior_words)
                else:
                    label_context = " ".join(part for part in [*prior_words, prefix] if part).strip()
                if not label_context:
                    label_context = self._text_before_first_blank(line_text)
                if not label_context and suffix:
                    label_context = suffix
                if not label_context:
                    label_context = f"Field {order}"

                label_type = self._heuristic_type_for_text(label_context.lower())
                if label_type is not None:
                    field_type = label_type
                else:
                    field_type = (
                        self._heuristic_type_for_text(f"{label_context} {' '.join(next_words)} {suffix}".lower())
                        or DocumentField.FieldTypeEnum.TEXT
                    )
                fields.append(
                    DetectedField(
                        field_type=field_type,
                        label=self._normalize_label(self._clean_label(label_context)),
                        page=page_index,
                        x=field_x0,
                        y=page_height - y1,
                        width=max(36.0, field_x1 - field_x0),
                        height=line_height,
                        is_required=True,
                        detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
                        order=order,
                    )
                )
                order += 1
        return fields

    def _extract_short_label_fields(
        self,
        line_words: list[list[tuple[Any, ...]]],
        page_index: int,
        page_height: float,
        order_start: int,
    ) -> list[DetectedField]:
        fields: list[DetectedField] = []
        order = order_start
        for words in line_words:
            line_height = self._line_height_for_words(words)
            if line_height is None:
                continue
            line_text = " ".join(str(word[4]) for word in words).strip()
            if "_" in line_text or any(char in line_text for char in self.checkbox_chars):
                continue
            if not line_text.endswith(":"):
                continue
            if len(words) > 4:
                continue

            label = self._clean_label(line_text)
            if not self._is_caption(label):
                continue
            field_type = self._heuristic_type_for_text(label.lower()) or DocumentField.FieldTypeEnum.TEXT
            x1 = max(float(word[2]) for word in words)
            y1 = max(float(word[3]) for word in words)
            fields.append(
                DetectedField(
                    field_type=field_type,
                    label=self._normalize_label(label),
                    page=page_index,
                    x=x1 + 8.0,
                    y=page_height - y1,
                    width=160.0,
                    height=line_height,
                    is_required=True,
                    detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
                    order=order,
                )
            )
            order += 1
        return fields

    def _extract_unicode_checkbox_fields(
        self,
        line_words: list[list[tuple[Any, ...]]],
        page_index: int,
        page_height: float,
        order_start: int,
    ) -> list[DetectedField]:
        fields: list[DetectedField] = []
        order = order_start
        for words in line_words:
            for index, word in enumerate(words):
                token = str(word[4] or "")
                if token not in self.checkbox_chars:
                    continue
                x0, y0, x1, y1 = map(float, word[:4])
                label = self._clean_label(
                    " ".join(str(item[4]) for item in words if str(item[4] or "") not in self.checkbox_chars)
                )
                fields.append(
                    DetectedField(
                        field_type=DocumentField.FieldTypeEnum.CHECKBOX,
                        label=self._normalize_label(label or f"Option {order}"),
                        page=page_index,
                        x=x0,
                        y=page_height - y1,
                        width=x1 - x0,
                        height=y1 - y0,
                        is_required=True,
                        detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
                        order=order,
                    )
                )
                order += 1
        return fields

    def _extract_drawn_checkbox_fields(
        self,
        line_words: list[list[tuple[Any, ...]]],
        drawings: list[dict[str, Any]],
        page_index: int,
        page_height: float,
        order_start: int,
    ) -> list[DetectedField]:
        fields: list[DetectedField] = []
        order = order_start
        for drawing in drawings:
            rect = drawing.get("rect")
            if not rect:
                continue
            if not (8.0 <= rect.width <= 16.0 and 8.0 <= rect.height <= 16.0):
                continue
            if abs(rect.width - rect.height) > 3.0:
                continue

            label = self._label_for_checkbox(rect, line_words)
            fields.append(
                DetectedField(
                    field_type=DocumentField.FieldTypeEnum.CHECKBOX,
                    label=self._normalize_label(label or f"Option {order}"),
                    page=page_index,
                    x=rect.x0,
                    y=page_height - rect.y1,
                    width=rect.width,
                    height=rect.height,
                    is_required=True,
                    detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
                    order=order,
                )
            )
            order += 1
        return fields

    def _extract_labeled_horizontal_line_fields(
        self,
        line_words: list[list[tuple[Any, ...]]],
        drawings: list[dict[str, Any]],
        page_index: int,
        page_height: float,
        order_start: int,
    ) -> list[DetectedField]:
        fields: list[DetectedField] = []
        order = order_start
        vertical_lines = [
            drawing["rect"]
            for drawing in drawings
            if drawing.get("rect") and drawing["rect"].width <= 2.5 and drawing["rect"].height >= 18.0
        ]

        for drawing in drawings:
            rect = drawing.get("rect")
            if not rect:
                continue
            if rect.width < 40.0 or rect.height > 2.5:
                continue
            if rect.y1 <= HEADER_FOOTER_MARGIN or rect.y0 >= page_height - HEADER_FOOTER_MARGIN:
                continue
            if self._is_table_border(rect, vertical_lines):
                continue
            if self._underlines_text(rect, line_words):
                continue

            line_height = self._line_height_near_rect(rect, line_words)
            if line_height is None:
                continue

            label = self._label_for_horizontal_line(rect, line_words)
            if label and not self._is_caption(label):
                # Body copy that happens to run alongside a rule, not a caption for it.
                continue
            if not label and self._sits_above_text(rect, line_words):
                # Nothing names this rule and a paragraph opens directly beneath it, so it is
                # dividing the page rather than waiting to be written on. A rule that does have a
                # caption is a writing line whatever follows it, and prose often follows it.
                continue
            named = bool(label)
            if not label:
                # Nothing names this rule, so there is no evidence it is a signature line rather
                # than a line to write on - and a block of them is a free-text answer area, which
                # as signature boxes would ask a signer to sign the same thing six times. Text is
                # what an unnamed writing line collects unless its caption says otherwise.
                label = f"Field {order}"
                field_type = DocumentField.FieldTypeEnum.TEXT
            else:
                field_type = self._heuristic_type_for_text(label.lower()) or DocumentField.FieldTypeEnum.TEXT
            fields.append(
                DetectedField(
                    field_type=field_type,
                    label=self._normalize_label(label),
                    page=page_index,
                    x=rect.x0,
                    y=page_height - rect.y1,
                    width=rect.width,
                    height=line_height,
                    is_required=True,
                    detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
                    order=order,
                    is_named=named,
                )
            )
            order += 1
        return fields

    def _extract_table_cell_fields(
        self,
        line_words: list[list[tuple[Any, ...]]],
        drawings: list[dict[str, Any]],
        page_index: int,
        page_height: float,
        order_start: int,
    ) -> list[DetectedField]:
        """Find the blank cells of a form laid out as a table.

        A form drawn as a table names what it wants in one cell and leaves the neighbouring cell
        empty to write in. The rules that bound those cells are exactly the table borders the line
        extractor has to ignore, so the cells are read from the grid instead of from the rules.

        A cell is offered as a field only when it is empty, which is what separates a form from a
        table that is simply presenting information - there, every cell already has content.
        """
        verticals = [d["rect"] for d in drawings if self._is_grid_rule(d.get("rect"), vertical=True)]
        rows = self._grid_lines(
            [d["rect"] for d in drawings if self._is_grid_rule(d.get("rect"), vertical=False)], horizontal=True
        )
        if len(verticals) < 2 or len(rows) < 3:
            return []

        words = [
            (float(word[0]), float(word[1]), float(word[2]), float(word[3]), str(word[4] or ""))
            for line in line_words
            for word in line
        ]

        fields: list[DetectedField] = []
        order = order_start
        # The headings of the table currently being read, if it began with a row of them.
        column_headings: list[tuple[float, str]] = []
        for top, bottom in zip(rows, rows[1:]):
            if not MIN_CELL_HEIGHT <= bottom - top <= MAX_CELL_HEIGHT:
                continue
            # Only the rules that run the height of this row divide it. A page can hold more than
            # one table, and taking every vertical on the page would cut each table's rows at the
            # other's column edges.
            columns = self._grid_lines(
                [
                    rect
                    for rect in verticals
                    if rect.y0 <= top + GRID_LINE_TOLERANCE and rect.y1 >= bottom - GRID_LINE_TOLERANCE
                ],
                horizontal=False,
            )
            if len(columns) < 3:
                continue
            cells = [
                (left, right, self._text_within(words, left, right, top, bottom))
                for left, right in zip(columns, columns[1:])
                if right - left >= MIN_CELL_WIDTH
            ]
            captions = [text for _, _, text in cells if text]

            if len(captions) == len(cells):
                # Every cell is full. Either the table is presenting information, or this is the
                # row of column headings above a table meant to be filled in - which is the
                # commonest form a table on a paper form takes, and which produced nothing at all
                # while a row had to be part full to be read.
                headings = [self._clean_label(text) for _, _, text in cells]
                if all(self._is_caption(heading) for heading in headings):
                    column_headings = list(zip((left for left, _, _ in cells), headings))
                continue

            if not captions:
                # Nothing names the row. If a row of headings stands above it, each empty cell is
                # asking for what its own column was named.
                for left, right, _ in cells:
                    heading = self._heading_for_column(column_headings, left)
                    if not heading:
                        continue
                    fields.append(self._cell_field(heading, left, right, top, bottom, page_height, page_index, order))
                    order += 1
                continue

            label = self._clean_label(captions[0])
            if not self._is_caption(label):
                continue

            for left, right, text in cells:
                if text:
                    continue
                fields.append(self._cell_field(label, left, right, top, bottom, page_height, page_index, order))
                order += 1
        return fields

    @staticmethod
    def _heading_for_column(headings: list[tuple[float, str]], left: float) -> str:
        """The heading of the column this cell sits in, matched by where the column starts."""
        for heading_left, heading in headings:
            if abs(heading_left - left) <= GRID_LINE_TOLERANCE:
                return heading
        return ""

    def _cell_field(
        self,
        label: str,
        left: float,
        right: float,
        top: float,
        bottom: float,
        page_height: float,
        page_index: int,
        order: int,
    ) -> DetectedField:
        return DetectedField(
            field_type=self._heuristic_type_for_text(label.lower()) or DocumentField.FieldTypeEnum.TEXT,
            label=self._normalize_label(label),
            page=page_index,
            x=left + CELL_PADDING,
            y=page_height - bottom + CELL_PADDING,
            width=right - left - CELL_PADDING * 2,
            height=bottom - top - CELL_PADDING * 2,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
            order=order,
        )

    @staticmethod
    def _is_grid_rule(rect: fitz.Rect | None, *, vertical: bool) -> bool:
        if not rect:
            return False
        if vertical:
            return rect.width <= 2.5 and rect.height >= MIN_CELL_HEIGHT
        return rect.height <= 2.5 and rect.width >= MIN_CELL_WIDTH

    @staticmethod
    def _grid_lines(rects: list[fitz.Rect], *, horizontal: bool) -> list[float]:
        """Collapse the rules of a grid into the distinct positions they sit at.

        A border drawn cell by cell repeats the same position once per cell, and two rules meant
        to be the same line can be a fraction of a point apart.
        """
        positions = sorted(rect.y0 if horizontal else rect.x0 for rect in rects)
        collapsed: list[float] = []
        for position in positions:
            if not collapsed or position - collapsed[-1] > GRID_LINE_TOLERANCE:
                collapsed.append(position)
        return collapsed

    @staticmethod
    def _text_within(
        words: list[tuple[float, float, float, float, str]],
        left: float,
        right: float,
        top: float,
        bottom: float,
    ) -> str:
        inside = [
            token
            for x0, y0, x1, y1, token in words
            if token.strip() and left <= (x0 + x1) / 2 <= right and top <= (y0 + y1) / 2 <= bottom
        ]
        return " ".join(inside)

    def _label_for_checkbox(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]]) -> str:
        checkbox_center_y = (rect.y0 + rect.y1) / 2
        nearby_words: list[tuple[float, float, float, float, str]] = []
        for words in line_words:
            for word in words:
                x0, y0, x1, y1 = map(float, word[:4])
                token = str(word[4] or "")
                center_y = (y0 + y1) / 2
                if abs(center_y - checkbox_center_y) > 10.0:
                    continue
                if x1 < rect.x0 - 180.0 or x0 > rect.x1 + 340.0:
                    continue
                if token in self.checkbox_chars:
                    continue
                nearby_words.append((x0, y0, x1, y1, token))

        if not nearby_words:
            return ""

        left_words = [item for item in nearby_words if item[2] <= rect.x0 + 2.0]
        right_words = [item for item in nearby_words if item[0] >= rect.x1 - 2.0]

        left_phrase_words = [token for _, _, _, _, token in left_words if token.lower() not in {"tenant", "landlord"}]
        left_phrase = " ".join(left_phrase_words[-4:]).strip()
        right_phrase = " ".join(token for _, _, _, _, token in right_words[:8]).strip()
        label = self._clean_label(" ".join(part for part in [left_phrase, right_phrase] if part))

        if label:
            return label

        return self._clean_label(" ".join(token for _, _, _, _, token in nearby_words))

    def _line_height_for_words(self, words: list[tuple[Any, ...]]) -> float | None:
        """Use the PDF text bounds so inferred fields match the source line height."""
        heights = [float(word[3]) - float(word[1]) for word in words]
        return max(heights) if heights else None

    def _line_height_near_rect(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]]) -> float | None:
        line_center_y = (rect.y0 + rect.y1) / 2
        lines_with_metrics = [words for words in line_words if self._line_height_for_words(words) is not None]
        if not lines_with_metrics:
            return None
        closest_line = min(
            lines_with_metrics,
            key=lambda words: abs(
                ((min(float(word[1]) for word in words) + max(float(word[3]) for word in words)) / 2) - line_center_y
            ),
        )
        return self._line_height_for_words(closest_line)

    def _label_for_horizontal_line(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]]) -> str:
        """Find the caption to the left of a ruled writing space.

        The rule is drawn a little below the baseline of the row it belongs to, which can leave it
        nearer the caption of the row underneath than its own. Text sitting above the rule is
        therefore preferred over text merely centred on it, so a signature line is not labelled
        with the name of the line below it.
        """
        line_center_y = (rect.y0 + rect.y1) / 2
        above: list[tuple[float, str]] = []
        beside: list[tuple[float, str]] = []
        beneath: list[tuple[float, str]] = []
        for words in line_words:
            alongside: list[tuple[float, float, float, float, str]] = []
            overhead: list[tuple[float, float, float, float, str]] = []
            underneath: list[tuple[float, float, float, float, str]] = []
            for word in words:
                x0, y0, x1, y1 = map(float, word[:4])
                gap = rect.y0 - y1
                # Sitting over the rule, either written across it on the line above or set off to
                # its left in a label column. How far off varies by an order of magnitude between
                # documents, so distance is not used to judge it: a label column reaches 174pt in
                # one packet, and a heading that means something else sits 169pt away in another.
                if 0.0 <= gap <= ABOVE_CAPTION_GAP and x0 <= rect.x1:
                    overhead.append((x0, y0, x1, y1, str(word[4])))
                # Standing beside it: a label column, which ends before the rule begins. The band
                # is wider than a row's own height because the rule sits below its caption's
                # baseline; the closest caption still wins, so admitting more costs nothing.
                if abs((y0 + y1) / 2 - line_center_y) <= BESIDE_CAPTION_BAND and x1 <= rect.x0 + 8.0:
                    alongside.append((x0, y0, x1, y1, str(word[4])))
                # Written under the rule and starting where it starts, which is how a signature
                # block names its lines. Alignment is what identifies it rather than distance:
                # the caption of the row below also sits under a rule, but begins at the margin
                # rather than at the rule, so the two do not look alike.
                if (
                    0.0 <= y0 - rect.y1 <= BELOW_CAPTION_GAP
                    and abs(x0 - rect.x0) <= BELOW_CAPTION_ALIGNMENT
                    and x0 <= rect.x1
                ):
                    underneath.append((x0, y0, x1, y1, str(word[4])))

            overhead_run = self._caption_run(overhead)
            if overhead_run:
                above.append(
                    (rect.y0 - max(word[3] for word in overhead_run), " ".join(word[4] for word in overhead_run))
                )

            beside_run = self._caption_run(alongside)
            if beside_run:
                centre = (min(word[1] for word in beside_run) + max(word[3] for word in beside_run)) / 2
                beside.append((abs(centre - line_center_y), " ".join(word[4] for word in beside_run)))

            if underneath:
                run = sorted(underneath, key=lambda word: word[0])
                beneath.append((run[0][1] - rect.y1, " ".join(word[4] for word in run)))

        # A caption under the rule and aligned to it is unambiguous, so it settles the matter
        # before anything to the left is considered. Without that, a rule takes the caption of the
        # line to its left however far away it is - which is how "Signature" came to name the date
        # line two columns over, leaving the line people actually sign on unnamed.
        ranked = sorted(above) or sorted(beneath) or sorted(beside)
        return self._clean_label(ranked[0][1]) if ranked else ""

    @staticmethod
    def _caption_run(
        candidates: list[tuple[float, float, float, float, str]],
    ) -> list[tuple[float, float, float, float, str]]:
        """Take the run of words nearest the rule, stopping where the row's layout breaks.

        A caption can be any length, so counting words truncates it - "Social Security Number"
        becomes "Security Number", and "Date of Birth" becomes a fragment that then reads as
        prose. Reading leftwards from the rule and stopping at the first wide gap keeps the whole
        caption while leaving behind whatever sits in an earlier column of the same row.
        """
        ordered = sorted(candidates, key=lambda word: word[0])
        run: list[tuple[float, float, float, float, str]] = []
        for word in reversed(ordered):
            if run and run[0][0] - word[2] > CAPTION_WORD_GAP:
                break
            run.insert(0, word)
        return run

    def _text_before_first_blank(self, line_text: str) -> str:
        match = self.underscore_pattern.search(line_text)
        if not match:
            return ""
        return line_text[: match.start()].strip(" :.-")

    def _clean_label(self, value: str) -> str:
        cleaned = re.sub(r"_+", "", str(value or ""))
        cleaned = cleaned.replace("☐", "").replace("□", "")
        cleaned = cleaned.replace("\u200b", "")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip(" :.-")

    def _merge_prefix_with_prior_words(self, prefix: str, prior_words: list[str]) -> str:
        normalized_prefix = self._clean_label(prefix).lower()
        cleaned_prior = [self._clean_label(word) for word in prior_words if self._clean_label(word)]
        if not cleaned_prior:
            return prefix

        if normalized_prefix == "name" and cleaned_prior[-1].lower() in {"print", "printed"}:
            return f"{cleaned_prior[-1]} {prefix}"

        if normalized_prefix == "signature":
            for word in reversed(cleaned_prior):
                lowered = word.lower()
                if lowered in {
                    "tenant",
                    "tenant’s",
                    "tenant's",
                    "landlord",
                    "landlord’s",
                    "landlord's",
                    "agent",
                    "agent’s",
                    "agent's",
                }:
                    return f"{word} {prefix}"

        return prefix

    def _underlines_text(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]]) -> bool:
        """Check whether the rule is the underline of the text directly on top of it."""
        return self._touches_text(rect, line_words, above=True)

    def _sits_above_text(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]]) -> bool:
        """Check whether text opens immediately beneath the rule."""
        return self._touches_text(rect, line_words, above=False)

    def _touches_text(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]], *, above: bool) -> bool:
        for words in line_words:
            for word in words:
                x0, y0, x1, y1 = map(float, word[:4])
                token = str(word[4] or "")
                if "_" in token or token in self.checkbox_chars:
                    continue
                horizontal_overlap = min(x1, rect.x1) - max(x0, rect.x0)
                if horizontal_overlap <= 12.0:
                    continue
                distance = abs(y1 - rect.y0) if above else abs(y0 - rect.y1)
                if distance <= 4.0:
                    return True
        return False

    @staticmethod
    def _merge_written_answer_lines(candidates: list[DetectedField]) -> list[DetectedField]:
        """Collapse a ruled block written for one answer into one field.

        Several rules of the same width, evenly spaced and named by nothing, are the space left
        for a single written answer. Kept apart they ask for that answer a line at a time, so a
        sentence has to be broken across separate inputs to fit, and each line is offered as its
        own thing to fill in. Rules that carry their own captions are left alone: a column of them
        is a form, and merging those would lose every label on it.
        """
        unnamed = sorted(
            (c for c in candidates if not c.is_named and c.field_type == DocumentField.FieldTypeEnum.TEXT),
            key=lambda field: -field.y,
        )
        merged: list[DetectedField] = []
        block: list[DetectedField] = []

        def flush() -> None:
            if len(block) < MIN_ANSWER_BLOCK_LINES:
                return
            top, bottom = block[0], block[-1]
            top.field_type = DocumentField.FieldTypeEnum.MULTILINE
            top.label = "Written answer"
            top.height = top.y + top.height - bottom.y
            top.y = bottom.y
            merged.append(top)
            for line in block[1:]:
                merged.append(line)

        for line in unnamed:
            if block and (
                abs(line.x - block[-1].x) > GRID_LINE_TOLERANCE
                or abs(line.width - block[-1].width) > GRID_LINE_TOLERANCE
                or not MIN_CELL_HEIGHT <= block[-1].y - line.y <= MAX_ANSWER_LINE_PITCH
            ):
                flush()
                block = []
            block.append(line)
        flush()

        absorbed = {id(field) for field in merged}
        kept = [field for field in candidates if id(field) not in absorbed]
        return kept + [field for field in merged if field.field_type == DocumentField.FieldTypeEnum.MULTILINE]

    @staticmethod
    def _settle_heuristic_fields(candidates: list[DetectedField]) -> list[DetectedField]:
        """Apply what can honestly be concluded about a field read off a printed page.

        Requiredness is not written on a page. A printed line gives its position and, through its
        caption, what it collects - nothing about it says an answer is compulsory, and assuming so
        is worse than leaving it open. A pair of tick boxes reading YES and NO cannot both be
        ticked, so requiring both stops the document being submitted at all. Only a place to sign
        speaks for itself: a signature line exists to be signed. The rest is the administrator's
        to mark, which they can do in the editor.

        A writing line is also given room to be written in. Its height comes from the text nearest
        it, which measures the caption's own glyphs rather than the space left to answer in, and a
        value set in a nine-point box next to one set in a table cell reads as two different
        documents.
        """
        for candidate in candidates:
            candidate.is_required = candidate.field_type in (
                DocumentField.FieldTypeEnum.SIGNATURE,
                DocumentField.FieldTypeEnum.INITIALS,
            )
            if candidate.field_type in (
                DocumentField.FieldTypeEnum.TEXT,
                DocumentField.FieldTypeEnum.MULTILINE,
                DocumentField.FieldTypeEnum.SIGNATURE,
                DocumentField.FieldTypeEnum.INITIALS,
            ):
                candidate.height = max(candidate.height, MIN_WRITING_HEIGHT)
        return candidates

    @staticmethod
    def _is_caption(label: str) -> bool:
        """Check that a caption names something rather than continuing a sentence.

        Prose reaching the end of a line, or broken by a colon mid-sentence, reads as a caption to
        anything looking only at where the words sit. A caption names a thing being asked for, so it
        opens its own phrase and closes it: one starting mid-sentence in lower case belongs to the
        paragraph around it, and one hinging on a joining word is a clause taken out of that
        paragraph rather than the name of a field.
        """
        if not label or label[:1].islower():
            return False
        words = label.split()
        if not words:
            return False
        # A question put just before a writing line is the form asking for something, however it
        # is worded. "If something is missing, what is it?" opens on a joining word and reads as a
        # clause by every other measure here, and it is still a question with a line to answer it.
        if label.rstrip().endswith("?"):
            return True
        return words[0].lower() not in CLAUSE_WORDS and words[-1].lower() not in CLAUSE_WORDS

    def _is_table_border(self, rect: fitz.Rect, vertical_lines: list[fitz.Rect]) -> bool:
        intersections = 0
        for vertical in vertical_lines:
            if rect.x0 - 2.0 <= vertical.x0 <= rect.x1 + 2.0 and vertical.y0 <= rect.y0 <= vertical.y1:
                intersections += 1
            if intersections >= 2:
                return True
        return False

    def _deduplicate_fields(self, fields: list[DetectedField]) -> list[DetectedField]:
        deduped: list[DetectedField] = []
        for field in sorted(fields, key=lambda item: (item.page, item.y, item.x, item.width)):
            duplicate = False
            for existing in deduped:
                if field.page != existing.page:
                    continue
                if self._rects_overlap(field, existing) >= 0.8:
                    duplicate = True
                    if len(field.label) > len(existing.label):
                        existing.label = field.label
                    break
            if not duplicate:
                deduped.append(field)
        return deduped

    def _rects_overlap(self, left: DetectedField, right: DetectedField) -> float:
        left_x1 = left.x + left.width
        left_y1 = left.y + left.height
        right_x1 = right.x + right.width
        right_y1 = right.y + right.height

        intersection_width = max(0.0, min(left_x1, right_x1) - max(left.x, right.x))
        intersection_height = max(0.0, min(left_y1, right_y1) - max(left.y, right.y))
        intersection_area = intersection_width * intersection_height
        if intersection_area <= 0:
            return 0.0

        left_area = left.width * left.height
        right_area = right.width * right.height
        return intersection_area / min(left_area, right_area)
