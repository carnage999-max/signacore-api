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
    ANCHOR_TAG_NOT_PLACED = "ANCHOR_TAG_NOT_PLACED"
    FIELD_FORMATTING_NOT_APPLIED = "FIELD_FORMATTING_NOT_APPLIED"
    HIDDEN_FIELD_IGNORED = "HIDDEN_FIELD_IGNORED"
    NO_FIELDS_DETECTED = "NO_FIELDS_DETECTED"
    NO_SUPPORTED_FIELDS_IMPORTED = "NO_SUPPORTED_FIELDS_IMPORTED"
    SCANNED_DOCUMENT_NO_TEXT_LAYER = "SCANNED_DOCUMENT_NO_TEXT_LAYER"
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
FLAG_COMB = 1 << 24
FLAG_RICH_TEXT = 1 << 25

# PDF 32000-1 table 165: annotation flags.
ANNOT_FLAG_HIDDEN = 1 << 1
ANNOT_FLAG_NO_VIEW = 1 << 5

# PDF 32000-1 table 197: additional-action slots.
# A slot either changes the value the field holds, or only how it is shown while typing.
VALUE_AFFECTING_SLOTS = ("C", "V")
PRESENTATIONAL_SLOTS = ("F", "K", "Fo", "Bl")
IMAGE_BUTTON_ACTION = "buttonimporticon"
PARENT_CHAIN_LIMIT = 8
MAX_LABEL_LENGTH = 255
MAX_DERIVED_LABEL_LENGTH = 60

INTERNAL_PATH_PATTERN = re.compile(r"\[\d+\]|\w\.\w|subform", re.IGNORECASE)
LABEL_START_PATTERN = re.compile(r"^[A-Za-z0-9(]")
ITEM_NUMBER_PATTERN = re.compile(r"^(\d+[a-z]?)\s*[.)]?\s+(?=\S)")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=\.)\s+(?=[A-Z])")
INITIALS_PATTERN = re.compile(r"\binitials?\b", re.IGNORECASE)
SIGNATURE_LABEL_PATTERN = re.compile(r"\b(signature|sign here|signed by)\b", re.IGNORECASE)
INSTRUCTION_PATTERN = re.compile(
    r"^(note|see|if|for|caution|example|check|enter|complete|do not|you must)\b",
    re.IGNORECASE,
)

SIDE_LABEL_GAP = 110.0
SIDE_LABEL_BAND = 6.0
CAPTION_SEARCH_GAP = 44.0
CAPTION_CANDIDATE_LIMIT = 4
WRAPPED_LABEL_LINE_LIMIT = 3
WRAPPED_LABEL_TOLERANCE = 26.0
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


def widget_max_length(document: fitz.Document, widget: fitz.Widget) -> int | None:
    """Read ``/MaxLen``, which like ``/Ff`` may be inherited from a parent field dictionary."""
    own = int(getattr(widget, "text_maxlen", 0) or 0)
    if own > 0:
        return own
    inherited = _inherited_int(document, widget.xref, "MaxLen")
    return inherited if inherited > 0 else None


def _inherited_int(document: fitz.Document, xref: int, key: str) -> int:
    """Read an integer key from a widget or the first ancestor field that declares it."""
    for _ in range(PARENT_CHAIN_LIMIT):
        try:
            entry = document.xref_get_key(xref, key)
        except Exception:
            return 0
        if entry[0] == "int":
            try:
                return int(entry[1])
            except ValueError:
                return 0
        parent = document.xref_get_key(xref, "Parent")
        if parent[0] != "xref":
            return 0
        xref = int(parent[1].split()[0])
    return 0


def is_comb_widget(document: fitz.Document, widget: fitz.Widget, max_length: int | None) -> bool:
    """A comb field spreads its value one character per cell, so it needs ``/MaxLen``."""
    if not max_length:
        return False
    if widget_field_flags(widget) & FLAG_COMB:
        return True
    return bool(_inherited_int(document, widget.xref, "Ff") & FLAG_COMB)


def is_widget_hidden(document: fitz.Document, widget: fitz.Widget) -> bool:
    if int(getattr(widget, "field_display", 0) or 0):
        return True
    return bool(_annotation_flags(document, widget.xref) & (ANNOT_FLAG_HIDDEN | ANNOT_FLAG_NO_VIEW))


@dataclass
class WidgetScripts:
    """What a widget asks a viewer to run, split by whether it can change the value.

    Nothing here is executed. Scripts are read so a widget can be classified: a date mask or a
    grey-placeholder script does not change what a signer enters, while a calculation or a
    validation rule does.
    """

    action: str = ""
    value_affecting_slots: tuple[str, ...] = ()
    presentational_slots: tuple[str, ...] = ()

    @property
    def has_value_affecting_script(self) -> bool:
        return bool(self.value_affecting_slots)

    @property
    def has_presentational_script(self) -> bool:
        return bool(self.presentational_slots)

    @property
    def is_image_button(self) -> bool:
        return IMAGE_BUTTON_ACTION in self.action.lower()


def collect_widget_scripts(document: fitz.Document, widget: fitz.Widget) -> WidgetScripts:
    """Gather a widget's scripts, including any inherited from a parent field dictionary.

    PyMuPDF's ``script*`` properties read only the widget's own dictionary, so a script declared
    on a parent - which is how kid widgets normally carry one - would otherwise go unnoticed.
    """
    value_slots: list[str] = []
    presentational: list[str] = []
    action = _action_script(document, widget.xref)

    own_slots = {
        "C": getattr(widget, "script_calc", None),
        "V": getattr(widget, "script_change", None),
        "F": getattr(widget, "script_format", None),
        "K": getattr(widget, "script_stroke", None),
        "Bl": getattr(widget, "script_blur", None),
        "Fo": getattr(widget, "script_focus", None),
    }
    for slot, script in own_slots.items():
        if not script:
            continue
        (value_slots if slot in VALUE_AFFECTING_SLOTS else presentational).append(slot)

    xref = widget.xref
    for _ in range(PARENT_CHAIN_LIMIT):
        for slot in _additional_action_slots(document, xref):
            if slot in VALUE_AFFECTING_SLOTS and slot not in value_slots:
                value_slots.append(slot)
            elif slot in PRESENTATIONAL_SLOTS and slot not in presentational:
                presentational.append(slot)
        if not action:
            action = _action_script(document, xref)
        parent = document.xref_get_key(xref, "Parent")
        if parent[0] != "xref":
            break
        xref = int(parent[1].split()[0])

    return WidgetScripts(
        action=action,
        value_affecting_slots=tuple(value_slots),
        presentational_slots=tuple(presentational),
    )


def _additional_action_slots(document: fitz.Document, xref: int) -> list[str]:
    entry = _xref_key(document, xref, "AA")
    if entry[0] != "dict":
        return []
    return re.findall(r"/([A-Za-z]+)\s", entry[1])


def _action_script(document: fitz.Document, xref: int) -> str:
    entry = _xref_key(document, xref, "A")
    if entry[0] == "xref":
        try:
            return _javascript_text(document.xref_object(int(entry[1].split()[0]), compressed=True))
        except Exception:  # pragma: no cover - unreadable action carries no usable text
            return ""
    if entry[0] == "dict":
        return _javascript_text(entry[1])
    return ""


def _javascript_text(obj: str) -> str:
    """Extract a ``/JS`` payload as text. Reading is not running."""
    match = re.search(r"/JS\s*(<[0-9A-Fa-f]*>|\([^)]*\))", obj)
    if not match:
        return obj
    raw = match.group(1)
    if raw.startswith("("):
        return raw[1:-1]
    try:
        data = bytes.fromhex(raw[1:-1])
    except ValueError:
        return ""
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace")
    return data.decode("latin-1", "replace")


def _xref_key(document: fitz.Document, xref: int, key: str):
    try:
        return document.xref_get_key(xref, key)
    except Exception:  # pragma: no cover - unreadable object has no keys
        return ("null", "null")


def classify_widget(document: fitz.Document, widget: fitz.Widget, label: str) -> tuple[str | None, str | None]:
    """Return ``(field_type, warning_code)``.

    A warning without a type means the widget was skipped. A type *with* a warning means the
    field was imported but something about it could not be carried over.
    """
    flags = widget_field_flags(widget)
    widget_type = int(getattr(widget, "field_type", -1))
    is_pushbutton = widget_type == fitz.PDF_WIDGET_TYPE_BUTTON or bool(flags & FLAG_PUSHBUTTON)
    scripts = collect_widget_scripts(document, widget)

    if is_widget_hidden(document, widget):
        return None, ImportWarningEnum.HIDDEN_FIELD_IGNORED

    if is_pushbutton:
        return _classify_button(scripts, label)

    if scripts.action or scripts.has_value_affecting_script:
        # A calculation or a validation rule decides the value; a plain action is untrusted.
        return None, ImportWarningEnum.UNSUPPORTED_PDF_JAVASCRIPT

    # Formatting, keystroke, focus and blur scripts only affect how a value is shown while it is
    # being typed, so the field is imported and the lost formatting is reported instead.
    formatting_warning = ImportWarningEnum.FIELD_FORMATTING_NOT_APPLIED if scripts.has_presentational_script else None

    if widget_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON or flags & FLAG_RADIO:
        return None, ImportWarningEnum.UNSUPPORTED_RADIO_GROUP

    if widget_type in (fitz.PDF_WIDGET_TYPE_LISTBOX, fitz.PDF_WIDGET_TYPE_COMBOBOX) or flags & FLAG_COMBO:
        return None, ImportWarningEnum.UNSUPPORTED_CHOICE_FIELD

    if flags & FLAG_READ_ONLY:
        return None, ImportWarningEnum.UNSUPPORTED_READ_ONLY_FIELD

    if widget_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
        return DocumentField.FieldTypeEnum.CHECKBOX, formatting_warning

    if widget_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
        return DocumentField.FieldTypeEnum.SIGNATURE, formatting_warning

    if widget_type == fitz.PDF_WIDGET_TYPE_TEXT:
        if flags & (FLAG_PASSWORD | FLAG_FILE_SELECT | FLAG_RICH_TEXT):
            return None, ImportWarningEnum.UNSUPPORTED_WIDGET_TYPE
        if flags & FLAG_MULTILINE:
            return DocumentField.FieldTypeEnum.MULTILINE, formatting_warning
        if INITIALS_PATTERN.search(label):
            return DocumentField.FieldTypeEnum.INITIALS, formatting_warning
        return DocumentField.FieldTypeEnum.TEXT, formatting_warning

    return None, ImportWarningEnum.UNSUPPORTED_WIDGET_TYPE


def _classify_button(scripts: WidgetScripts, label: str) -> tuple[str | None, str | None]:
    """An image-import button on a signature line is where a signature belongs.

    Acrobat builds a signature placeholder as a push button whose action imports an icon. Its
    action is never run; SignaCore collects its own signature at the same rectangle. The label
    has to agree, so a logo placeholder is not turned into a signature field.
    """
    if not scripts.is_image_button:
        return None, ImportWarningEnum.UNSUPPORTED_ACTION_BUTTON
    if INITIALS_PATTERN.search(label):
        return DocumentField.FieldTypeEnum.INITIALS, None
    if SIGNATURE_LABEL_PATTERN.search(label):
        return DocumentField.FieldTypeEnum.SIGNATURE, None
    return None, ImportWarningEnum.UNSUPPORTED_ACTION_BUTTON


def resolve_label(
    widget: fitz.Widget,
    text_lines: list[TextLine],
    page_index: int,
    field_index: int,
) -> tuple[str, bool]:
    """Build a customer-facing label, never exposing an internal AcroForm/XFA field path.

    Returns the label and whether it came from the document itself, so a caller can fall back
    to a neighbouring field's caption instead of a positional placeholder.
    """
    tooltip = _clean(str(getattr(widget, "field_label", "") or ""))
    if tooltip and not _is_internal_path(tooltip):
        return tooltip[:MAX_LABEL_LENGTH], True

    for candidate in _label_candidates(
        widget.rect,
        text_lines,
        prefer_right=_prefers_following_label(widget),
        prefer_side=_prefers_side_label(widget),
    ):
        shortened = _shorten(candidate)
        if _is_confident_label(shortened):
            return shortened, True

    field_name = _clean(str(getattr(widget, "field_name", "") or "").replace("_", " "))
    if field_name and not _is_internal_path(field_name) and len(field_name) <= MAX_DERIVED_LABEL_LENGTH:
        return field_name[:1].upper() + field_name[1:], False

    return f"Page {page_index} field {field_index}", False


def _prefers_following_label(widget: fitz.Widget) -> bool:
    """Check boxes are labelled by the text that follows them."""
    return int(getattr(widget, "field_type", -1)) == fitz.PDF_WIDGET_TYPE_CHECKBOX


def _prefers_side_label(widget: fitz.Widget) -> bool:
    """Signature boxes are labelled beside them, however wide they are.

    A signature box in a table row is wide enough to trigger the caption-above preference, which
    then reads whatever heading happens to sit above the table.
    """
    widget_type = int(getattr(widget, "field_type", -1))
    if widget_type in (fitz.PDF_WIDGET_TYPE_SIGNATURE, fitz.PDF_WIDGET_TYPE_BUTTON):
        return True
    return bool(widget_field_flags(widget) & FLAG_PUSHBUTTON)


def _label_candidates(
    rect: fitz.Rect,
    text_lines: list[TextLine],
    *,
    prefer_right: bool,
    prefer_side: bool = False,
) -> list[str]:
    """Ordered label guesses.

    Wide entry boxes are captioned on the line above, while narrow ones are labelled by the text
    beside them - reading a wide box's neighbour picks up whatever body copy sits alongside it.
    """
    right = _adjacent_line(rect, text_lines, to_right=True)
    left = _adjacent_line(rect, text_lines, to_right=False)
    beside = [right, left] if prefer_right else [left, right]
    above = _caption_lines_above(rect, text_lines)

    if not prefer_right and not prefer_side and rect.width >= WIDE_FIELD_WIDTH:
        ordered = [*above, *beside]
    else:
        ordered = [*beside, *above]
    return [text for text in (_clean(line.text) for line in ordered if line is not None) if text]


def _adjacent_line(rect: fitz.Rect, text_lines: list[TextLine], *, to_right: bool) -> TextLine | None:
    """The text beside a field, joined when the label wraps onto more than one line.

    A label in a table cell often wraps - "Signature of Principal or / Authorized Official:" -
    and reading only the nearest line loses half of it.
    """
    center_y = (rect.y0 + rect.y1) / 2
    band_top = min(rect.y0, center_y - SIDE_LABEL_BAND)
    band_bottom = max(rect.y1, center_y + SIDE_LABEL_BAND)

    matches: list[tuple[float, TextLine]] = []
    for line in text_lines:
        line_center = (line.y0 + line.y1) / 2
        if not band_top <= line_center <= band_bottom:
            continue
        gap = line.x0 - rect.x1 if to_right else rect.x0 - line.x1
        if 0 <= gap < SIDE_LABEL_GAP:
            matches.append((gap, line))
    if not matches:
        return None

    nearest_gap = min(gap for gap, _ in matches)
    wrapped = [line for gap, line in matches if gap - nearest_gap < WRAPPED_LABEL_TOLERANCE]
    wrapped.sort(key=lambda line: line.y0)
    wrapped = wrapped[:WRAPPED_LABEL_LINE_LIMIT]

    joined = " ".join(line.text.strip() for line in wrapped if line.text.strip())
    first = wrapped[0]
    return TextLine(x0=first.x0, y0=first.y0, x1=max(line.x1 for line in wrapped), y1=wrapped[-1].y1, text=joined)


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
