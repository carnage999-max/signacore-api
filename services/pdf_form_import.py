"""Classification and labelling of native PDF form widgets.

Nothing in this module executes, evaluates, or stores PDF JavaScript or action content. Actions
are detected only so the widget carrying them can be handled safely, never run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import fitz

from apps.documents.models import DocumentField


class ImportWarningEnum:
    HIDDEN_FIELD_IGNORED = "HIDDEN_FIELD_IGNORED"
    NO_SUPPORTED_FIELDS_IMPORTED = "NO_SUPPORTED_FIELDS_IMPORTED"
    SIGNATURE_FIELD_NOT_DETECTED = "SIGNATURE_FIELD_NOT_DETECTED"
    UNSUPPORTED_ACTION_BUTTON = "UNSUPPORTED_ACTION_BUTTON"
    UNSUPPORTED_CHOICE_FIELD = "UNSUPPORTED_CHOICE_FIELD"
    UNSUPPORTED_PDF_JAVASCRIPT = "UNSUPPORTED_PDF_JAVASCRIPT"
    UNSUPPORTED_RADIO_GROUP = "UNSUPPORTED_RADIO_GROUP"
    UNSUPPORTED_READ_ONLY_FIELD = "UNSUPPORTED_READ_ONLY_FIELD"
    UNSUPPORTED_WIDGET_TYPE = "UNSUPPORTED_WIDGET_TYPE"


# PDF 32000-1 tables 227-228: field flags.
FLAG_READ_ONLY = 1 << 0
FLAG_REQUIRED = 1 << 1
FLAG_MULTILINE = 1 << 12
FLAG_PASSWORD = 1 << 13
FLAG_RADIO = 1 << 15
FLAG_PUSHBUTTON = 1 << 16
FLAG_COMBO = 1 << 17
FLAG_FILE_SELECT = 1 << 20
FLAG_RICH_TEXT = 1 << 25

# PDF 32000-1 table 165: annotation flags.
ANNOT_FLAG_HIDDEN = 1 << 1
ANNOT_FLAG_NO_VIEW = 1 << 5

ACTION_KEYS = ("A", "AA")
PARENT_CHAIN_LIMIT = 8
MAX_LABEL_LENGTH = 255
MAX_DERIVED_LABEL_LENGTH = 60

INTERNAL_PATH_PATTERN = re.compile(r"\[\d+\]|\w\.\w|subform", re.IGNORECASE)
LABEL_START_PATTERN = re.compile(r"^[A-Za-z0-9(]")
ITEM_NUMBER_PATTERN = re.compile(r"^(\d+[a-z]?)\s*[.)]?\s+(?=\S)")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=\.)\s+(?=[A-Z])")
INITIALS_PATTERN = re.compile(r"\binitials?\b", re.IGNORECASE)
INSTRUCTION_PATTERN = re.compile(
    r"^(note|see|if|for|caution|example|check|enter|complete|do not|you must)\b",
    re.IGNORECASE,
)

SIDE_LABEL_GAP = 110.0
SIDE_LABEL_BAND = 6.0
CAPTION_SEARCH_GAP = 44.0
CAPTION_CANDIDATE_LIMIT = 4
WIDE_FIELD_WIDTH = 120.0


@dataclass
class ImportReport:
    source: str
    native_widget_count: int = 0
    imported_field_count: int = 0
    ignored_widget_count: int = 0
    warning_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "native_widget_count": self.native_widget_count,
            "imported_field_count": self.imported_field_count,
            "ignored_widget_count": self.ignored_widget_count,
            "warning_codes": sorted(set(self.warning_codes)),
        }


@dataclass
class TextLine:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


def collect_text_lines(page: fitz.Page) -> list[TextLine]:
    grouped: dict[tuple[int, int], list[tuple[Any, ...]]] = {}
    for word in page.get_text("words"):
        grouped.setdefault((int(word[5]), int(word[6])), []).append(word)

    lines: list[TextLine] = []
    for words in grouped.values():
        words.sort(key=lambda item: float(item[0]))
        lines.append(
            TextLine(
                x0=min(float(word[0]) for word in words),
                y0=min(float(word[1]) for word in words),
                x1=max(float(word[2]) for word in words),
                y1=max(float(word[3]) for word in words),
                text=" ".join(str(word[4]) for word in words),
            )
        )
    return lines


def widget_field_flags(widget: fitz.Widget) -> int:
    return int(getattr(widget, "field_flags", 0) or 0)


def is_widget_required(widget: fitz.Widget) -> bool:
    return bool(widget_field_flags(widget) & FLAG_REQUIRED)


def is_widget_hidden(document: fitz.Document, widget: fitz.Widget) -> bool:
    if int(getattr(widget, "field_display", 0) or 0):
        return True
    return bool(_annotation_flags(document, widget.xref) & (ANNOT_FLAG_HIDDEN | ANNOT_FLAG_NO_VIEW))


def has_active_behavior(document: fitz.Document, widget: fitz.Widget) -> bool:
    """Detect actions or scripts on the widget or anywhere up its inherited field chain.

    PyMuPDF's ``script*`` properties read only the widget's own dictionary, so a validation
    script declared on a parent field dictionary - which is how kid widgets normally carry
    one - would otherwise go unnoticed.
    """
    for attribute in (
        "script",
        "script_stroke",
        "script_format",
        "script_change",
        "script_calc",
        "script_blur",
        "script_focus",
    ):
        if getattr(widget, attribute, None):
            return True

    xref = widget.xref
    for _ in range(PARENT_CHAIN_LIMIT):
        keys = _xref_keys(document, xref)
        if not keys:
            return False
        if any(key in keys for key in ACTION_KEYS):
            return True
        parent = document.xref_get_key(xref, "Parent")
        if parent[0] != "xref":
            return False
        xref = int(parent[1].split()[0])
    return False


def classify_widget(document: fitz.Document, widget: fitz.Widget, label: str) -> tuple[str | None, str | None]:
    """Return ``(field_type, warning_code)``; exactly one of the pair is ever set."""
    flags = widget_field_flags(widget)
    widget_type = int(getattr(widget, "field_type", -1))
    is_pushbutton = widget_type == fitz.PDF_WIDGET_TYPE_BUTTON or bool(flags & FLAG_PUSHBUTTON)

    if is_widget_hidden(document, widget):
        return None, ImportWarningEnum.HIDDEN_FIELD_IGNORED

    if has_active_behavior(document, widget):
        if is_pushbutton:
            return None, ImportWarningEnum.UNSUPPORTED_ACTION_BUTTON
        return None, ImportWarningEnum.UNSUPPORTED_PDF_JAVASCRIPT

    if is_pushbutton:
        return None, ImportWarningEnum.UNSUPPORTED_ACTION_BUTTON

    if widget_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON or flags & FLAG_RADIO:
        return None, ImportWarningEnum.UNSUPPORTED_RADIO_GROUP

    if widget_type in (fitz.PDF_WIDGET_TYPE_LISTBOX, fitz.PDF_WIDGET_TYPE_COMBOBOX) or flags & FLAG_COMBO:
        return None, ImportWarningEnum.UNSUPPORTED_CHOICE_FIELD

    if flags & FLAG_READ_ONLY:
        return None, ImportWarningEnum.UNSUPPORTED_READ_ONLY_FIELD

    if widget_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
        return DocumentField.FieldTypeEnum.CHECKBOX, None

    if widget_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
        return DocumentField.FieldTypeEnum.SIGNATURE, None

    if widget_type == fitz.PDF_WIDGET_TYPE_TEXT:
        if flags & (FLAG_PASSWORD | FLAG_FILE_SELECT | FLAG_RICH_TEXT):
            return None, ImportWarningEnum.UNSUPPORTED_WIDGET_TYPE
        if flags & FLAG_MULTILINE:
            return DocumentField.FieldTypeEnum.MULTILINE, None
        if INITIALS_PATTERN.search(label):
            return DocumentField.FieldTypeEnum.INITIALS, None
        return DocumentField.FieldTypeEnum.TEXT, None

    return None, ImportWarningEnum.UNSUPPORTED_WIDGET_TYPE


def resolve_label(
    widget: fitz.Widget,
    text_lines: list[TextLine],
    page_index: int,
    field_index: int,
) -> str:
    """Build a customer-facing label, never exposing an internal AcroForm/XFA field path."""
    tooltip = _clean(str(getattr(widget, "field_label", "") or ""))
    if tooltip and not _is_internal_path(tooltip):
        return tooltip[:MAX_LABEL_LENGTH]

    is_checkbox = int(getattr(widget, "field_type", -1)) == fitz.PDF_WIDGET_TYPE_CHECKBOX
    for candidate in _label_candidates(widget.rect, text_lines, prefer_right=is_checkbox):
        shortened = _shorten(candidate)
        if _is_confident_label(shortened):
            return shortened

    field_name = _clean(str(getattr(widget, "field_name", "") or "").replace("_", " "))
    if field_name and not _is_internal_path(field_name) and len(field_name) <= MAX_DERIVED_LABEL_LENGTH:
        return field_name[:1].upper() + field_name[1:]

    return f"Page {page_index} field {field_index}"


def _label_candidates(rect: fitz.Rect, text_lines: list[TextLine], *, prefer_right: bool) -> list[str]:
    """Ordered label guesses.

    Check boxes are labelled by the text that follows them. Wide entry boxes are captioned on
    the line above, while narrow ones are labelled by the text beside them - reading a wide
    box's neighbour picks up whatever body copy happens to sit alongside it.
    """
    right = _adjacent_line(rect, text_lines, to_right=True)
    left = _adjacent_line(rect, text_lines, to_right=False)
    beside = [right, left] if prefer_right else [left, right]
    above = _caption_lines_above(rect, text_lines)

    if not prefer_right and rect.width >= WIDE_FIELD_WIDTH:
        ordered = [*above, *beside]
    else:
        ordered = [*beside, *above]
    return [text for text in (_clean(line.text) for line in ordered if line is not None) if text]


def _adjacent_line(rect: fitz.Rect, text_lines: list[TextLine], *, to_right: bool) -> TextLine | None:
    center_y = (rect.y0 + rect.y1) / 2
    matches = []
    for line in text_lines:
        if abs((line.y0 + line.y1) / 2 - center_y) > SIDE_LABEL_BAND:
            continue
        gap = line.x0 - rect.x1 if to_right else rect.x0 - line.x1
        if 0 <= gap < SIDE_LABEL_GAP:
            matches.append((gap, line))
    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


def _caption_lines_above(rect: fitz.Rect, text_lines: list[TextLine]) -> list[TextLine]:
    """Lines above the field, nearest first.

    Wrapped body copy is rejected later by the confidence gate, which is what lets a numbered
    caption ("1 Name of entity") win over the continuation line sitting directly above it.
    """
    above = [
        line
        for line in text_lines
        if line.y1 <= rect.y0 + 1.5
        and rect.y0 - line.y1 < CAPTION_SEARCH_GAP
        and min(line.x1, rect.x1) - max(line.x0, rect.x0) > 4.0
    ]
    above.sort(key=lambda line: rect.y0 - line.y1)
    return above[:CAPTION_CANDIDATE_LIMIT]


def _shorten(value: str) -> str:
    """Reduce a caption to its label clause: first sentence, then first phrase if still long.

    Returns an empty string when the text is still over-long after splitting, because prose
    that cannot be reduced to a clause is body copy rather than a field caption.
    """
    text = ITEM_NUMBER_PATTERN.sub("", _clean(value))
    text = _clean(SENTENCE_SPLIT_PATTERN.split(text)[0])
    for separator in (";", ","):
        if len(text) > MAX_DERIVED_LABEL_LENGTH and separator in text:
            text = _clean(text.split(separator)[0])
    if len(text) > MAX_DERIVED_LABEL_LENGTH:
        return ""
    return text


def _is_confident_label(candidate: str) -> bool:
    """Accept only label-shaped text so paragraph fragments never become field labels."""
    if not 2 <= len(candidate) <= MAX_DERIVED_LABEL_LENGTH:
        return False
    if not LABEL_START_PATTERN.match(candidate):
        return False
    if candidate[0].islower():
        return False
    if INSTRUCTION_PATTERN.match(candidate):
        return False
    return not _is_internal_path(candidate)


def _is_internal_path(value: str) -> bool:
    return bool(INTERNAL_PATH_PATTERN.search(value))


def _clean(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    return cleaned.strip(" :.-•")


def _xref_keys(document: fitz.Document, xref: int) -> list[str]:
    try:
        return list(document.xref_get_keys(xref) or [])
    except Exception:
        return []


def _annotation_flags(document: fitz.Document, xref: int) -> int:
    try:
        flag_key = document.xref_get_key(xref, "F")
    except Exception:
        return 0
    if flag_key[0] != "int":
        return 0
    try:
        return int(flag_key[1])
    except ValueError:
        return 0
