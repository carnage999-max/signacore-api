from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import fitz

from apps.documents.models import DocumentField


@dataclass(frozen=True)
class AuthoredRenderField:
    field_type: str
    label: str
    page: int
    x: float
    y: float
    width: float
    height: float
    is_required: bool
    order: int


class AuthoredPDFRenderer:
    """Render authored JSON and field coordinates in one deterministic pass."""

    page_sizes = {
        "LETTER": (612.0, 792.0),
        "A4": (595.28, 841.89),
    }
    supported_nodes = {
        "doc",
        "paragraph",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "blockquote",
        "hardBreak",
        "signacoreField",
        "text",
    }
    field_sizes = {
        DocumentField.FieldTypeEnum.SIGNATURE: (190.0, 38.0),
        DocumentField.FieldTypeEnum.INITIALS: (92.0, 32.0),
        DocumentField.FieldTypeEnum.TEXT: (180.0, 24.0),
    }
    max_text_length = 10000
    max_nodes = 300
    body_font = "helv"
    heading_font = "hebo"
    text_color = (0.08, 0.1, 0.13)

    def validate(self, content: Any) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise ValueError("Document content must be an object.")
        if content.get("type") != "doc":
            raise ValueError("Document content must start with a document node.")

        attrs = content.get("attrs") or {}
        page_size = str(attrs.get("pageSize", "LETTER")).upper()
        if page_size not in self.page_sizes:
            raise ValueError("Choose Letter or A4 page size.")

        nodes = content.get("content", [])
        if not isinstance(nodes, list):
            raise ValueError("Document content must contain a list of blocks.")
        if len(nodes) > self.max_nodes:
            raise ValueError("This document contains too many blocks.")
        for node in nodes:
            self._validate_node(node)
        return content

    def render(self, content: Any) -> tuple[bytes, list[AuthoredRenderField]]:
        content = self.validate(content)
        page_size = str((content.get("attrs") or {}).get("pageSize", "LETTER")).upper()
        page_width, page_height = self.page_sizes[page_size]
        margin = 54.0
        document = fitz.open()
        fields: list[AuthoredRenderField] = []
        page = document.new_page(width=page_width, height=page_height)
        cursor_y = margin
        page_number = 1
        order = 1

        def ensure_space(required_height: float) -> None:
            nonlocal page, cursor_y, page_number
            if cursor_y + required_height <= page_height - margin:
                return
            page = document.new_page(width=page_width, height=page_height)
            cursor_y = margin
            page_number += 1

        def insert_text(
            text: str,
            x: float,
            width: float,
            font_size: float,
            line_height: float,
            font_name: str = "",
        ) -> None:
            """Draw each wrapped line on its own baseline.

            ``insert_textbox`` writes nothing at all when the text does not fit the rectangle
            it is given, which silently emptied whole documents. Drawing line by line also lets
            a block continue onto the next page instead of being moved or dropped whole.
            """
            nonlocal cursor_y
            for line in self._wrap_text(text, font_size, width, font_name or self.body_font):
                ensure_space(line_height)
                page.insert_text(
                    fitz.Point(x, cursor_y + font_size),
                    line,
                    fontsize=font_size,
                    fontname=font_name or self.body_font,
                    color=self.text_color,
                )
                cursor_y += line_height

        for node in content.get("content", []):
            node_type = node.get("type")
            if node_type == "hardBreak":
                cursor_y += 18
                continue

            if node_type == "signacoreField":
                field_type = str((node.get("attrs") or {}).get("fieldType", "TEXT")).upper()
                width, height = self.field_sizes[field_type]
                ensure_space(height + 18)
                field_width = min(width, page_width - (margin * 2))
                field = AuthoredRenderField(
                    field_type=field_type,
                    label=self._field_label(node),
                    page=page_number,
                    x=margin,
                    # DocumentField stores a bottom-left origin; the cursor runs from the top.
                    y=page_height - cursor_y - height,
                    width=field_width,
                    height=height,
                    is_required=bool((node.get("attrs") or {}).get("required", True)),
                    order=order,
                )
                fields.append(field)
                rule_y = cursor_y + height - 3
                page.draw_line(
                    fitz.Point(field.x, rule_y),
                    fitz.Point(field.x + field.width, rule_y),
                    color=(0.55, 0.59, 0.64),
                    width=0.7,
                )
                cursor_y += height + 18
                order += 1
                continue

            text = self._node_text(node)
            if node_type in {"bulletList", "orderedList"}:
                for index, item in enumerate(node.get("content", []) or [], start=1):
                    prefix = "- " if node_type == "bulletList" else f"{index}. "
                    insert_text(
                        prefix + self._node_text(item),
                        margin + 12,
                        page_width - (margin * 2) - 12,
                        11.0,
                        17.0,
                    )
                cursor_y += 5
                continue

            if not text.strip():
                cursor_y += 10
                continue

            if node_type == "heading":
                level = int((node.get("attrs") or {}).get("level", 2))
                font_size = {1: 24.0, 2: 18.0, 3: 14.0}.get(level, 14.0)
                insert_text(
                    text,
                    margin,
                    page_width - (margin * 2),
                    font_size,
                    font_size * 1.25,
                    self.heading_font,
                )
                cursor_y += 8
                continue

            if node_type == "blockquote":
                ensure_space(46)
                page.draw_rect(
                    fitz.Rect(margin, cursor_y, margin + 3, cursor_y + 40),
                    color=(0.12, 0.53, 0.8),
                    fill=(0.12, 0.53, 0.8),
                )
                insert_text(
                    text,
                    margin + 14,
                    page_width - (margin * 2) - 14,
                    11.0,
                    17.0,
                )
                cursor_y += 8
                continue

            insert_text(
                text,
                margin,
                page_width - (margin * 2),
                11.0,
                17.0,
            )
            cursor_y += 8

        pdf_bytes = document.tobytes(garbage=4, deflate=True)
        document.close()
        return pdf_bytes, fields

    @staticmethod
    def _wrap_text(text: str, font_size: float, width: float, font_name: str = "helv") -> list[str]:
        lines: list[str] = []
        for paragraph in str(text).splitlines() or [""]:
            current = ""
            for word in re.split(r"\s+", paragraph.strip()):
                if not word:
                    continue
                candidate = f"{current} {word}".strip()
                if current and fitz.get_text_length(candidate, fontname=font_name, fontsize=font_size) > width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
        return lines or [""]

    def _validate_node(self, node: Any) -> None:
        if not isinstance(node, dict) or node.get("type") not in self.supported_nodes:
            raise ValueError("This document contains an unsupported block.")
        node_type = node["type"]
        if node_type == "text" and len(str(node.get("text", ""))) > self.max_text_length:
            raise ValueError("A text block is too long.")
        if node_type == "signacoreField":
            attrs = node.get("attrs") or {}
            field_type = str(attrs.get("fieldType", "")).upper()
            if field_type not in self.field_sizes:
                raise ValueError("This signing field type is not supported.")
            if len(self._field_label(node)) > 255:
                raise ValueError("Signing field labels must be 255 characters or fewer.")
        for child in node.get("content", []) or []:
            self._validate_node(child)

    @staticmethod
    def _field_label(node: dict[str, Any]) -> str:
        label = str((node.get("attrs") or {}).get("label", "Field")).strip()
        return label[:255] or "Field"

    def _node_text(self, node: dict[str, Any]) -> str:
        if node.get("type") == "text":
            return str(node.get("text", ""))
        if node.get("type") == "hardBreak":
            return "\n"
        return "".join(self._node_text(child) for child in node.get("content", []) or [])
