from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
    text_keywords = ("name", "date", "employee")

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
                        label=widget.field_label or widget.field_name or f"Field {order}",
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
            blocks = page.get_text("blocks")
            drawings = page.get_drawings()

            for drawing in drawings:
                rect = drawing.get("rect")
                if not rect:
                    continue
                if rect.width > 100 and rect.height <= 2:
                    detected_fields.append(
                        DetectedField(
                            field_type=DocumentField.FieldTypeEnum.SIGNATURE,
                            label=f"Signature {order}",
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

            for block in blocks:
                x0, y0, x1, y1, text, *_ = block
                normalized = (text or "").strip().lower()
                if not normalized:
                    continue
                field_type = self._heuristic_type_for_text(normalized)
                if not field_type:
                    continue
                detected_fields.append(
                    DetectedField(
                        field_type=field_type,
                        label=text.strip().rstrip(":"),
                        page=page_index,
                        x=x1 + 8.0,
                        y=page_height - y1,
                        width=160.0,
                        height=max(24.0, y1 - y0),
                        is_required=True,
                        detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
                        order=order,
                    )
                )
                order += 1
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
        if "sig" in candidate:
            return DocumentField.FieldTypeEnum.SIGNATURE
        if "initial" in candidate:
            return DocumentField.FieldTypeEnum.INITIALS
        return DocumentField.FieldTypeEnum.TEXT

    def _is_widget_required(self, widget: fitz.Widget) -> bool:
        flags = int(getattr(widget, "field_flags", 0) or 0)
        return bool(flags & (1 << 1))

    def _heuristic_type_for_text(self, text: str) -> str | None:
        if any(keyword in text for keyword in self.initials_keywords):
            return DocumentField.FieldTypeEnum.INITIALS
        if any(keyword in text for keyword in self.signature_keywords):
            return DocumentField.FieldTypeEnum.SIGNATURE
        if any(keyword in text for keyword in self.text_keywords) or text.endswith(":"):
            return DocumentField.FieldTypeEnum.TEXT
        return None
