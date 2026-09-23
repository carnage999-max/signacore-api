"""Render authored documents to PDF and report where each signing field landed.

Layout runs through ReportLab's Platypus engine, which handles pagination, tables and
keep-together rules that a hand-written renderer cannot. Field positions are captured during
layout by a flowable that records its own drawn rectangle, so a field's stored coordinates are
the ones the engine actually used rather than a second, independent calculation.

ReportLab's canvas origin is bottom-left, which is the origin ``DocumentField`` stores, so a
captured position needs no conversion.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    ListFlowable,
    ListItem,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

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


class SigningFieldFlowable(Flowable):
    """Reserve space for a signing field and record where the engine placed it.

    Platypus decides the final page and position, so the flowable reports its own rectangle at
    draw time rather than the caller predicting it.
    """

    def __init__(
        self,
        field_type: str,
        label: str,
        is_required: bool,
        order: int,
        width: float,
        height: float,
    ) -> None:
        super().__init__()
        self.field_type = field_type
        self.label = label
        self.is_required = is_required
        self.order = order
        self.width = width
        self.height = height
        self.placement: AuthoredRenderField | None = None

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self.width = min(self.width, available_width)
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        origin_x, origin_y = canvas.absolutePosition(0, 0)
        self.placement = AuthoredRenderField(
            field_type=self.field_type,
            label=self.label,
            page=canvas.getPageNumber(),
            x=origin_x,
            y=origin_y,
            width=self.width,
            height=self.height,
            is_required=self.is_required,
            order=self.order,
        )

        canvas.saveState()
        canvas.setStrokeColor(colors.Color(0.55, 0.59, 0.64))
        canvas.setLineWidth(0.7)
        canvas.line(0, 0, self.width, 0)
        canvas.setFillColor(colors.Color(0.45, 0.5, 0.56))
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(0, -10, self.label[:80])
        canvas.restoreState()


class HorizontalRule(Flowable):
    def __init__(self, width: float) -> None:
        super().__init__()
        self.width = width
        self.height = 1.0

    def draw(self) -> None:
        self.canv.setStrokeColor(colors.Color(0.82, 0.85, 0.88))
        self.canv.setLineWidth(0.8)
        self.canv.line(0, 0, self.width, 0)


class AuthoredPDFRenderer:
    """Render authored JSON and field coordinates in one deterministic pass."""

    page_sizes = {"LETTER": LETTER, "A4": A4}
    supported_nodes = {
        "doc",
        "paragraph",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "blockquote",
        "hardBreak",
        "horizontalRule",
        "table",
        "tableRow",
        "tableCell",
        "tableHeader",
        "signacoreField",
        "text",
    }
    field_sizes = {
        DocumentField.FieldTypeEnum.SIGNATURE: (190.0, 38.0),
        DocumentField.FieldTypeEnum.INITIALS: (92.0, 32.0),
        DocumentField.FieldTypeEnum.TEXT: (180.0, 24.0),
        DocumentField.FieldTypeEnum.MULTILINE: (320.0, 60.0),
    }
    alignments = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT, "justify": TA_JUSTIFY}
    heading_sizes = {1: 22.0, 2: 16.0, 3: 13.0}
    margin = 54.0
    body_font_size = 10.5
    body_leading = 15.5
    max_text_length = 10000
    max_nodes = 300
    max_table_columns = 12

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
        page_size = self.page_sizes[str((content.get("attrs") or {}).get("pageSize", "LETTER")).upper()]

        buffer = BytesIO()
        document = BaseDocTemplate(
            buffer,
            pagesize=page_size,
            leftMargin=self.margin,
            rightMargin=self.margin,
            topMargin=self.margin,
            bottomMargin=self.margin,
            title="SignaCore document",
        )
        frame = Frame(
            document.leftMargin,
            document.bottomMargin,
            document.width,
            document.height,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        document.addPageTemplates([PageTemplate(id="body", frames=[frame])])

        field_flowables: list[SigningFieldFlowable] = []
        story = self._build_story(content.get("content", []) or [], document.width, field_flowables)
        document.build(story or [Spacer(1, 1)])

        return buffer.getvalue(), [flowable.placement for flowable in field_flowables if flowable.placement is not None]

    def _build_story(
        self,
        nodes: list[Any],
        available_width: float,
        fields: list[SigningFieldFlowable],
    ) -> list[Any]:
        story: list[Any] = []
        for node in nodes:
            story.extend(self._render_node(node, available_width, fields))
        return story

    def _render_node(
        self,
        node: Any,
        available_width: float,
        fields: list[SigningFieldFlowable],
    ) -> list[Any]:
        if not isinstance(node, dict):
            return []

        node_type = node.get("type")
        if node_type == "signacoreField":
            return self._render_field(node, available_width, fields)
        if node_type == "heading":
            return self._render_heading(node)
        if node_type == "paragraph":
            return self._render_paragraph(node)
        if node_type in {"bulletList", "orderedList"}:
            return self._render_list(node, node_type, available_width, fields)
        if node_type == "blockquote":
            return self._render_blockquote(node, available_width, fields)
        if node_type == "table":
            return self._render_table(node, available_width, fields)
        if node_type == "horizontalRule":
            return [Spacer(1, 6), HorizontalRule(available_width), Spacer(1, 10)]
        if node_type == "hardBreak":
            return [Spacer(1, 10)]
        return []

    def _render_field(
        self,
        node: dict[str, Any],
        available_width: float,
        fields: list[SigningFieldFlowable],
    ) -> list[Any]:
        attrs = node.get("attrs") or {}
        field_type = str(attrs.get("fieldType", "TEXT")).upper()
        width, height = self.field_sizes[field_type]
        flowable = SigningFieldFlowable(
            field_type=field_type,
            label=self._field_label(node),
            is_required=bool(attrs.get("required", True)),
            order=len(fields) + 1,
            width=min(width, available_width),
            height=height,
        )
        fields.append(flowable)
        # The caption is drawn below the rule, so the next block has to clear it.
        return [Spacer(1, 10), flowable, Spacer(1, 22)]

    def _render_heading(self, node: dict[str, Any]) -> list[Any]:
        markup = self._inline_markup(node)
        if not markup.strip():
            return []

        level = int((node.get("attrs") or {}).get("level", 2))
        size = self.heading_sizes.get(level, 13.0)
        style = ParagraphStyle(
            name=f"heading{level}",
            fontName="Helvetica-Bold",
            fontSize=size,
            leading=size * 1.25,
            spaceBefore=0 if level == 1 else 14,
            spaceAfter=7,
            textColor=colors.Color(0.05, 0.07, 0.1),
            alignment=self._alignment(node),
        )
        return [Paragraph(markup, style)]

    def _render_paragraph(self, node: dict[str, Any]) -> list[Any]:
        markup = self._inline_markup(node)
        if not markup.strip():
            return [Spacer(1, 8)]
        return [Paragraph(markup, self._body_style(node))]

    def _body_style(self, node: dict[str, Any]) -> ParagraphStyle:
        return ParagraphStyle(
            name="body",
            fontName="Helvetica",
            fontSize=self.body_font_size,
            leading=self.body_leading,
            spaceAfter=8,
            textColor=colors.Color(0.08, 0.1, 0.13),
            alignment=self._alignment(node),
        )

    def _render_list(
        self,
        node: dict[str, Any],
        node_type: str,
        available_width: float,
        fields: list[SigningFieldFlowable],
    ) -> list[Any]:
        items: list[ListItem] = []
        for child in node.get("content", []) or []:
            if not isinstance(child, dict):
                continue
            item_story = self._build_story(child.get("content", []) or [], available_width - 20, fields)
            if item_story:
                items.append(ListItem(item_story, leftIndent=18))
        if not items:
            return []

        is_ordered = node_type == "orderedList"
        return [
            ListFlowable(
                items,
                bulletType="1" if is_ordered else "bullet",
                start=int((node.get("attrs") or {}).get("start", 1) or 1) if is_ordered else None,
                bulletFontName="Helvetica",
                bulletFontSize=self.body_font_size,
                leftIndent=20,
            ),
            Spacer(1, 6),
        ]

    def _render_blockquote(
        self,
        node: dict[str, Any],
        available_width: float,
        fields: list[SigningFieldFlowable],
    ) -> list[Any]:
        inner = self._build_story(node.get("content", []) or [], available_width - 24, fields)
        if not inner:
            return []

        quote = Table([[inner]], colWidths=[available_width])
        quote.setStyle(
            TableStyle(
                [
                    ("LINEBEFORE", (0, 0), (0, -1), 3, colors.Color(0.12, 0.53, 0.8)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 14),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        return [quote, Spacer(1, 8)]

    def _render_table(
        self,
        node: dict[str, Any],
        available_width: float,
        fields: list[SigningFieldFlowable],
    ) -> list[Any]:
        rows: list[list[Any]] = []
        has_header_row = False

        for row in node.get("content", []) or []:
            if not isinstance(row, dict) or row.get("type") != "tableRow":
                continue
            cells: list[Any] = []
            header_cells = 0
            for cell in row.get("content", []) or []:
                if not isinstance(cell, dict) or cell.get("type") not in {"tableCell", "tableHeader"}:
                    continue
                if cell.get("type") == "tableHeader":
                    header_cells += 1
                cell_story = self._build_story(cell.get("content", []) or [], available_width, fields)
                cells.append(cell_story or [Spacer(1, 1)])
            if not cells:
                continue
            if not rows and header_cells == len(cells):
                has_header_row = True
            rows.append(cells)

        if not rows:
            return []

        column_count = min(max(len(row) for row in rows), self.max_table_columns)
        for row in rows:
            del row[column_count:]
            while len(row) < column_count:
                row.append([Spacer(1, 1)])

        style_commands = [
            ("GRID", (0, 0), (-1, -1), 0.5, colors.Color(0.78, 0.82, 0.86)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        if has_header_row:
            style_commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.95, 0.96, 0.98)))

        table = Table(
            rows,
            colWidths=[available_width / column_count] * column_count,
            repeatRows=1 if has_header_row else 0,
        )
        table.setStyle(TableStyle(style_commands))
        return [table, Spacer(1, 12)]

    def _alignment(self, node: dict[str, Any]) -> int:
        alignment = str((node.get("attrs") or {}).get("textAlign", "") or "").lower()
        return self.alignments.get(alignment, TA_LEFT)

    def _inline_markup(self, node: dict[str, Any]) -> str:
        """Convert a node's inline children into ReportLab's inline markup.

        Text is escaped before any tag is applied, so document content cannot introduce markup
        of its own.
        """
        parts: list[str] = []
        for child in node.get("content", []) or []:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "hardBreak":
                parts.append("<br/>")
                continue
            if child.get("type") != "text":
                parts.append(self._inline_markup(child))
                continue

            text = html.escape(str(child.get("text", "")))
            for mark in child.get("marks", []) or []:
                mark_type = str((mark or {}).get("type", ""))
                if mark_type == "bold":
                    text = f"<b>{text}</b>"
                elif mark_type == "italic":
                    text = f"<i>{text}</i>"
                elif mark_type == "underline":
                    text = f"<u>{text}</u>"
                elif mark_type == "strike":
                    text = f"<strike>{text}</strike>"
                elif mark_type == "code":
                    text = f'<font face="Courier">{text}</font>'
            parts.append(text)
        return "".join(parts)

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
        if node_type == "tableRow":
            cells = [cell for cell in node.get("content", []) or [] if isinstance(cell, dict)]
            if len(cells) > self.max_table_columns:
                raise ValueError(f"Tables can have at most {self.max_table_columns} columns.")

        for child in node.get("content", []) or []:
            self._validate_node(child)

    @staticmethod
    def _field_label(node: dict[str, Any]) -> str:
        label = str((node.get("attrs") or {}).get("label", "Field")).strip()
        return label[:255] or "Field"
