from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import fitz

from apps.documents.models import DocumentField


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


class PDFEngine:
    signature_keywords = ("signature",)
    initials_keywords = ("initials", "int.")
    text_keywords = ("name", "date", "email", "print", "tenant", "landlord", "rent", "term", "unit")
    max_label_length = 255
    checkbox_chars = ("☐", "□")
    underscore_pattern = re.compile(r"_{3,}")

    def analyse(self, pdf_path: str | Path) -> list[DetectedField]:
        document = fitz.open(pdf_path)
        try:
            fields = self._extract_acroform_fields(document)
            if fields:
                return fields
            return self.detect_heuristic(document)
        finally:
            document.close()

    def _extract_acroform_fields(self, document: fitz.Document) -> list[DetectedField]:
        detected_fields: list[DetectedField] = []
        order = 1
        for page_index, page in enumerate(document, start=1):
            widgets = list(page.widgets() or [])
            for widget in widgets:
                rect = widget.rect
                detected_fields.append(
                    DetectedField(
                        field_type=self._map_widget_type(widget),
                        label=self._normalize_label(
                            widget.field_label or widget.field_name or f"Field {order}"
                        ),
                        page=page_index,
                        x=rect.x0,
                        y=rect.y0,
                        width=rect.width,
                        height=rect.height,
                        is_required=self._is_widget_required(widget),
                        detection_source=DocumentField.DetectionSourceEnum.ACROFORM,
                        order=order,
                    )
                )
                order += 1
        return detected_fields

    def detect_heuristic(self, document: fitz.Document) -> list[DetectedField]:
        detected_fields: list[DetectedField] = []
        order = 1
        for page_index, page in enumerate(document, start=1):
            page_height = page.rect.height
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

            deduped_candidates = self._deduplicate_fields(page_candidates)
            for index, candidate in enumerate(deduped_candidates, start=order - len(page_candidates)):
                candidate.order = index
            detected_fields.extend(deduped_candidates)
            order = len(detected_fields) + 1
        return detected_fields

    def flatten(self, source_pdf: str | Path, output_pdf: str | Path, submissions: list[dict[str, Any]]) -> None:
        document = fitz.open(source_pdf)
        try:
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
                    page.insert_textbox(rect, submission["text_value"], fontsize=12)
                elif submission["value_type"] == "CHECKBOX":
                    if str(submission["text_value"]).lower() in {"true", "1", "yes", "on"}:
                        page.insert_textbox(
                            rect,
                            "X",
                            fontsize=max(12, min(rect.width, rect.height) * 0.95),
                            align=1,
                        )
                else:
                    page.insert_image(rect, filename=submission["image_path"])

            for page in document:
                widgets = list(page.widgets() or [])
                for widget in widgets:
                    page.delete_widget(widget)

            document.save(output_pdf)
        finally:
            document.close()

    def _map_widget_type(self, widget: fitz.Widget) -> str:
        field_type = str(getattr(widget, "field_type_string", "") or "").lower()
        field_name = str(getattr(widget, "field_name", "") or "").lower()
        candidate = f"{field_type} {field_name}"
        if "check" in candidate:
            return DocumentField.FieldTypeEnum.CHECKBOX
        if "sig" in candidate:
            return DocumentField.FieldTypeEnum.SIGNATURE
        if "initial" in candidate:
            return DocumentField.FieldTypeEnum.INITIALS
        return DocumentField.FieldTypeEnum.TEXT

    def _is_widget_required(self, widget: fitz.Widget) -> bool:
        flags = int(getattr(widget, "field_flags", 0) or 0)
        return bool(flags & (1 << 1))

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
        for word in page.get_text("words"):
            grouped.setdefault((int(word[5]), int(word[6])), []).append(word)
        return [sorted(words, key=lambda item: (item[1], item[0])) for _, words in sorted(grouped.items())]

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
                    field_type = self._heuristic_type_for_text(
                        f"{label_context} {' '.join(next_words)} {suffix}".lower()
                    ) or DocumentField.FieldTypeEnum.TEXT
                fields.append(
                    DetectedField(
                        field_type=field_type,
                        label=self._normalize_label(self._clean_label(label_context)),
                        page=page_index,
                        x=field_x0,
                        y=page_height - y1,
                        width=max(36.0, field_x1 - field_x0),
                        height=max(22.0, y1 - y0 + 8.0),
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
            line_text = " ".join(str(word[4]) for word in words).strip()
            if "_" in line_text or any(char in line_text for char in self.checkbox_chars):
                continue
            if not line_text.endswith(":"):
                continue
            if len(words) > 4:
                continue

            label = self._clean_label(line_text)
            field_type = self._heuristic_type_for_text(label.lower()) or DocumentField.FieldTypeEnum.TEXT
            x1 = max(float(word[2]) for word in words)
            y0 = min(float(word[1]) for word in words)
            y1 = max(float(word[3]) for word in words)
            fields.append(
                DetectedField(
                    field_type=field_type,
                    label=self._normalize_label(label),
                    page=page_index,
                    x=x1 + 8.0,
                    y=page_height - y1,
                    width=160.0,
                    height=max(22.0, y1 - y0 + 8.0),
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
                    " ".join(
                        str(item[4])
                        for item in words
                        if str(item[4] or "") not in self.checkbox_chars
                    )
                )
                fields.append(
                    DetectedField(
                        field_type=DocumentField.FieldTypeEnum.CHECKBOX,
                        label=self._normalize_label(label or f"Option {order}"),
                        page=page_index,
                        x=x0,
                        y=page_height - y1,
                        width=max(12.0, x1 - x0),
                        height=max(12.0, y1 - y0),
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
            if drawing.get("rect")
            and drawing["rect"].width <= 2.5
            and drawing["rect"].height >= 18.0
        ]

        for drawing in drawings:
            rect = drawing.get("rect")
            if not rect:
                continue
            if rect.width < 40.0 or rect.height > 2.5:
                continue
            if self._is_table_border(rect, vertical_lines):
                continue
            if self._line_overlaps_text(rect, line_words):
                continue

            label = self._label_for_horizontal_line(rect, line_words)
            if not label:
                label = f"Signature {order}"
                field_type = DocumentField.FieldTypeEnum.SIGNATURE
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
                    height=24.0,
                    is_required=True,
                    detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
                    order=order,
                )
            )
            order += 1
        return fields

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

        left_phrase_words = [
            token
            for _, _, _, _, token in left_words
            if token.lower() not in {"tenant", "landlord"}
        ]
        left_phrase = " ".join(left_phrase_words[-4:]).strip()
        right_phrase = " ".join(token for _, _, _, _, token in right_words[:8]).strip()
        label = self._clean_label(" ".join(part for part in [left_phrase, right_phrase] if part))

        if label:
            return label

        return self._clean_label(" ".join(token for _, _, _, _, token in nearby_words))

    def _label_for_horizontal_line(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]]) -> str:
        candidates: list[str] = []
        line_center_y = (rect.y0 + rect.y1) / 2
        for words in line_words:
            left_words = []
            for word in words:
                x0, y0, x1, y1 = map(float, word[:4])
                center_y = (y0 + y1) / 2
                if abs(center_y - line_center_y) > 12.0:
                    continue
                if x1 <= rect.x0 + 8.0 and x1 >= rect.x0 - 220.0:
                    left_words.append(str(word[4]))
            if left_words:
                candidates.append(" ".join(left_words[-4:]))
        cleaned = self._clean_label(candidates[-1] if candidates else "")
        return cleaned

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

    def _line_overlaps_text(self, rect: fitz.Rect, line_words: list[list[tuple[Any, ...]]]) -> bool:
        for words in line_words:
            for word in words:
                x0, y0, x1, y1 = map(float, word[:4])
                token = str(word[4] or "")
                if "_" in token or token in self.checkbox_chars:
                    continue
                horizontal_overlap = min(x1, rect.x1) - max(x0, rect.x0)
                if horizontal_overlap <= 12.0:
                    continue
                if abs(y1 - rect.y0) <= 4.0 or abs(y0 - rect.y1) <= 4.0:
                    return True
        return False

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
