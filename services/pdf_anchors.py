"""Detection of SignaCore anchor tags written into a document's own text.

An author places a tag such as ``{{signature}}`` or ``{{text:Employee name}}`` where a field
belongs, and SignaCore turns each occurrence into a field at that exact position. This is the
only detection strategy that carries explicit author intent, so it takes precedence over both
native widgets and visual heuristics.

Tags are matched as literal text; nothing in the document is executed or evaluated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import fitz

from apps.documents.models import DocumentField

ANCHOR_PATTERN = re.compile(
    r"\{\{\s*(signature|initials|text|multiline|checkbox|date)\s*(?::\s*([^}|]{1,60}?))?\s*(\|\s*optional\s*)?\}\}",
    re.IGNORECASE,
)

TAG_FIELD_TYPES = {
    "signature": DocumentField.FieldTypeEnum.SIGNATURE,
    "initials": DocumentField.FieldTypeEnum.INITIALS,
    "text": DocumentField.FieldTypeEnum.TEXT,
    "date": DocumentField.FieldTypeEnum.TEXT,
    "multiline": DocumentField.FieldTypeEnum.MULTILINE,
    "checkbox": DocumentField.FieldTypeEnum.CHECKBOX,
}

# Tags mark a position, not an area, so each type gets a usable default size in points.
TAG_SIZES = {
    DocumentField.FieldTypeEnum.SIGNATURE: (170.0, 38.0),
    DocumentField.FieldTypeEnum.INITIALS: (64.0, 34.0),
    DocumentField.FieldTypeEnum.TEXT: (190.0, 18.0),
    DocumentField.FieldTypeEnum.MULTILINE: (260.0, 60.0),
    DocumentField.FieldTypeEnum.CHECKBOX: (12.0, 12.0),
}

DEFAULT_LABELS = {
    "signature": "Signature",
    "initials": "Initials",
    "text": "Text",
    "date": "Date",
    "multiline": "Comments",
    "checkbox": "Checkbox",
}


@dataclass
class AnchorMatch:
    page: int
    field_type: str
    label: str
    is_required: bool
    rect: fitz.Rect
    tag_text: str


def find_anchor_tags(document: fitz.Document) -> list[AnchorMatch]:
    """Locate every anchor tag in the document, in reading order per page."""
    matches: list[AnchorMatch] = []

    for page_index, page in enumerate(document, start=1):
        page_text = page.get_text("text")
        if "{{" not in page_text:
            continue

        seen_rects: set[tuple[int, int]] = set()
        for match in ANCHOR_PATTERN.finditer(page_text):
            tag_text = match.group(0)
            keyword = match.group(1).lower()
            label = (match.group(2) or "").strip() or DEFAULT_LABELS[keyword]
            is_required = match.group(3) is None
            field_type = TAG_FIELD_TYPES[keyword]

            for rect in page.search_for(tag_text):
                key = (round(rect.x0), round(rect.y0))
                if key in seen_rects:
                    continue
                seen_rects.add(key)
                matches.append(
                    AnchorMatch(
                        page=page_index,
                        field_type=field_type,
                        label=label[:255],
                        is_required=is_required,
                        rect=fitz.Rect(rect),
                        tag_text=tag_text,
                    )
                )

    matches.sort(key=lambda item: (item.page, round(item.rect.y0, 1), round(item.rect.x0, 1)))
    return matches


def anchor_field_rect(match: AnchorMatch) -> fitz.Rect:
    """Size the field by its type, anchored at the tag's top-left.

    The tag's own width reflects how long the tag text is, which says nothing about how big the
    field should be - a long ``{{checkbox:Accept terms}}`` must still yield a small check box.
    """
    width, height = TAG_SIZES[match.field_type]
    return fitz.Rect(match.rect.x0, match.rect.y0, match.rect.x0 + width, match.rect.y0 + height)


def conceal_anchor_tags(document: fitz.Document) -> None:
    """Remove anchor tag text from a document so it never appears in the completed file."""
    redacted = False
    for page in document:
        page_text = page.get_text("text")
        if "{{" not in page_text:
            continue
        for match in ANCHOR_PATTERN.finditer(page_text):
            for rect in page.search_for(match.group(0)):
                page.add_redact_annot(rect, fill=(1, 1, 1))
                redacted = True
        if redacted:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            redacted = False
