"""Deterministic PDF builders for form-import tests.

These exist so tests never depend on a developer's local files. Each builder returns PDF bytes
and documents the native control it is exercising.
"""

from __future__ import annotations

import fitz

PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0

FLAG_REQUIRED = 1 << 1
FLAG_MULTILINE = 1 << 12
FLAG_RADIO = 1 << 15
FLAG_PUSHBUTTON = 1 << 16
FLAG_COMBO = 1 << 17
FLAG_COMB = 1 << 24


def _new_document() -> tuple[fitz.Document, fitz.Page]:
    document = fitz.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    return document, page


def _to_bytes(document: fitz.Document) -> bytes:
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


def _text_widget(
    *,
    name: str,
    rect: fitz.Rect,
    label: str = "",
    flags: int = 0,
) -> fitz.Widget:
    widget = fitz.Widget()
    widget.field_name = name
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.rect = rect
    widget.field_flags = flags
    if label:
        widget.field_label = label
    return widget


def _retype_last_widget(
    document: fitz.Document, page: fitz.Page, *, field_type: str, flags: int, options: str = ""
) -> int:
    """Rewrite the most recently added widget's field keys.

    PyMuPDF refuses to create a standalone radio kid, so native control shapes are produced by
    writing the PDF field keys the classifier actually reads.
    """
    xref = list(page.widgets())[-1].xref
    document.xref_set_key(xref, "FT", field_type)
    document.xref_set_key(xref, "Ff", str(flags))
    if options:
        document.xref_set_key(xref, "Opt", options)
    return xref


def build_supported_acroform_pdf() -> bytes:
    """A required text field, a checkbox, and a native signature widget."""
    document, page = _new_document()

    page.add_widget(
        _text_widget(
            name="employee_name",
            label="Employee Name",
            rect=fitz.Rect(72, 144, 240, 168),
            flags=FLAG_REQUIRED,
        )
    )

    checkbox = fitz.Widget()
    checkbox.field_name = "agree"
    checkbox.field_label = "I agree"
    checkbox.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    checkbox.rect = fitz.Rect(72, 200, 86, 214)
    page.add_widget(checkbox)

    signature = fitz.Widget()
    signature.field_name = "employee_signature"
    signature.field_label = "Employee Signature"
    signature.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
    signature.rect = fitz.Rect(72, 260, 280, 300)
    page.add_widget(signature)

    return _to_bytes(document)


def build_radio_group_pdf() -> bytes:
    """Three radio kids of one group, as an exclusive choice control."""
    document, page = _new_document()
    for index, top in enumerate((200, 230, 260)):
        page.add_widget(_text_widget(name=f"delivery_choice_{index}", rect=fitz.Rect(72, top, 86, top + 14)))
        _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_RADIO | (1 << 14))
    return _to_bytes(document)


def build_choice_field_pdf() -> bytes:
    """A list box and a combo box - both /Ch controls SignaCore cannot represent."""
    document, page = _new_document()

    page.add_widget(_text_widget(name="month", rect=fitz.Rect(72, 144, 260, 210)))
    _retype_last_widget(document, page, field_type="/Ch", flags=0, options="[(January)(February)(March)]")

    page.add_widget(_text_widget(name="vehicle", rect=fitz.Rect(72, 240, 260, 264)))
    _retype_last_widget(document, page, field_type="/Ch", flags=FLAG_COMBO, options="[(Airplane)(Boat)]")

    return _to_bytes(document)


def build_action_button_pdf() -> bytes:
    """A push button carrying a URI action, as submit/reset/navigation buttons do."""
    document, page = _new_document()
    page.add_widget(_text_widget(name="submit", rect=fitz.Rect(72, 144, 180, 168)))
    xref = _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_PUSHBUTTON)
    document.xref_set_key(xref, "A", "<</S/URI/URI(https://example.invalid/collect)>>")
    return _to_bytes(document)


def build_hidden_field_pdf() -> bytes:
    """A hidden text field carrying a prefilled value that must never reach a signer."""
    document, page = _new_document()
    widget = _text_widget(name="hidden_token", rect=fitz.Rect(72, 144, 300, 168))
    widget.field_value = "prefilled-secret-value"
    page.add_widget(widget)
    document.xref_set_key(list(page.widgets())[-1].xref, "F", "6")
    return _to_bytes(document)


def build_javascript_calculation_pdf() -> bytes:
    """A text field whose validation script lives on its parent field dictionary."""
    document, page = _new_document()
    page.add_widget(_text_widget(name="total", rect=fitz.Rect(72, 144, 240, 168)))

    widget_xref = list(page.widgets())[-1].xref
    action_xref = document.get_new_xref()
    document.update_object(action_xref, "<</S/JavaScript/JS(AFSimple_Calculate\\('SUM', new Array\\('a','b'\\)\\);)>>")
    document.xref_set_key(widget_xref, "AA", f"<</C {action_xref} 0 R>>")
    return _to_bytes(document)


def build_multiline_pdf() -> bytes:
    document, page = _new_document()
    page.add_widget(
        _text_widget(
            name="comments",
            label="Comments",
            rect=fitz.Rect(72, 144, 500, 240),
            flags=FLAG_MULTILINE,
        )
    )
    return _to_bytes(document)


def build_read_only_pdf() -> bytes:
    document, page = _new_document()
    page.add_widget(
        _text_widget(
            name="reference",
            label="Reference",
            rect=fitz.Rect(72, 144, 240, 168),
            flags=1 << 0,
        )
    )
    return _to_bytes(document)


def build_mixed_form_pdf() -> bytes:
    """One supported text field alongside a radio, a choice list, and a push button."""
    document, page = _new_document()

    page.add_widget(
        _text_widget(
            name="full_name",
            label="Full Name",
            rect=fitz.Rect(72, 144, 300, 168),
            flags=FLAG_REQUIRED,
        )
    )

    page.add_widget(_text_widget(name="plan", rect=fitz.Rect(72, 200, 86, 214)))
    _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_RADIO)

    page.add_widget(_text_widget(name="country", rect=fitz.Rect(72, 240, 260, 264)))
    _retype_last_widget(document, page, field_type="/Ch", flags=FLAG_COMBO, options="[(Nigeria)(Ghana)]")

    page.add_widget(_text_widget(name="reset", rect=fitz.Rect(72, 290, 160, 314)))
    _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_PUSHBUTTON)

    return _to_bytes(document)


def build_unsupported_only_form_pdf() -> bytes:
    """Native widgets that are all unsupported, drawn over heuristic-looking page furniture.

    The underscores and drawn line would be picked up by heuristic detection, which must not
    run for a document that has native widgets.
    """
    document, page = _new_document()
    page.insert_text((72, 120), "Signature:________________________ Date:____________")
    shape = page.new_shape()
    shape.draw_line((72, 320), (300, 320))
    shape.finish(width=1)
    shape.commit()

    page.add_widget(_text_widget(name="choice", rect=fitz.Rect(72, 400, 260, 424)))
    _retype_last_widget(document, page, field_type="/Ch", flags=FLAG_COMBO, options="[(One)(Two)]")

    return _to_bytes(document)


def build_xfa_style_pdf() -> bytes:
    """A W-9-shaped document: XFA-style internal field names and a printed signature line.

    Field names mirror the IRS W-9's internal paths, no widget carries a tooltip, and the
    signature line is printed page content rather than a native signature widget.
    """
    document, page = _new_document()
    page.insert_text((58, 132), "1 Name of entity/individual", fontsize=9)
    page.insert_text((58, 192), "2 Business name/disregarded entity name", fontsize=9)
    page.insert_text((58, 300), "Sign Here", fontsize=9)
    page.insert_text((58, 316), "Signature of U.S. person", fontsize=9)

    page.add_widget(
        _text_widget(
            name="topmostSubform[0].Page1[0].f1_01[0]",
            rect=fitz.Rect(58, 136, 576, 150),
        )
    )
    page.add_widget(
        _text_widget(
            name="topmostSubform[0].Page1[0].f1_02[0]",
            rect=fitz.Rect(58, 196, 576, 210),
        )
    )
    page.add_widget(
        _text_widget(
            name="topmostSubform[0].Page1[0].f1_09[0]",
            rect=fitz.Rect(58, 230, 400, 268),
            flags=FLAG_MULTILINE,
        )
    )
    return _to_bytes(document)


def build_flat_pdf() -> bytes:
    """No native widgets at all, so heuristic detection is expected to run."""
    document, page = _new_document()
    page.insert_text((72, 120), "Name:")
    page.insert_text((72, 220), "Initials:")
    shape = page.new_shape()
    shape.draw_line((72, 320), (240, 320))
    shape.finish(width=1)
    shape.commit()
    return _to_bytes(document)


def build_active_content_pdf() -> bytes:
    """A document carrying page widgets, a link action, and document-level JavaScript."""
    document, page = _new_document()
    page.insert_text((72, 120), "Agreement")
    page.add_widget(_text_widget(name="signer_name", rect=fitz.Rect(72, 144, 300, 168)))
    page.insert_link(
        {
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(72, 300, 200, 320),
            "uri": "https://example.invalid/leak",
        }
    )

    action_xref = document.get_new_xref()
    document.update_object(action_xref, "<</S/JavaScript/JS(app.alert\\('hello'\\);)>>")
    document.xref_set_key(document.pdf_catalog(), "OpenAction", f"{action_xref} 0 R")
    return _to_bytes(document)


def build_comb_field_pdf() -> bytes:
    """A W-9-style split SSN row: three comb widgets with 3, 2 and 4 character cells."""
    document, page = _new_document()
    page.insert_text((417, 366), "Social security number", fontsize=9)

    for name, rect, cells in (
        ("ssn_area", fitz.Rect(417.6, 372.0, 460.8, 396.0), 3),
        ("ssn_group", fitz.Rect(475.2, 372.0, 504.0, 396.0), 2),
        ("ssn_serial", fitz.Rect(518.4, 372.0, 576.0, 396.0), 4),
    ):
        page.add_widget(_text_widget(name=name, rect=rect))
        xref = list(page.widgets())[-1].xref
        document.xref_set_key(xref, "Ff", str(FLAG_COMB))
        document.xref_set_key(xref, "MaxLen", str(cells))

    return _to_bytes(document)


def build_inherited_maxlen_pdf() -> bytes:
    """A comb widget whose /MaxLen and /Ff live on its parent field dictionary."""
    document, page = _new_document()
    page.add_widget(_text_widget(name="account", rect=fitz.Rect(72, 144, 240, 168)))
    widget_xref = list(page.widgets())[-1].xref

    parent_xref = document.get_new_xref()
    document.update_object(parent_xref, f"<</T(account_parent)/FT/Tx/Ff {FLAG_COMB}/MaxLen 6>>")
    document.xref_set_key(widget_xref, "Parent", f"{parent_xref} 0 R")
    # A kid widget that declares no flags of its own must inherit them from the parent field.
    document.xref_set_key(widget_xref, "Ff", "null")
    return _to_bytes(document)


def build_anchor_tag_pdf() -> bytes:
    """A flat document whose author placed SignaCore anchor tags in the text."""
    document, page = _new_document()
    page.insert_text((72, 120), "Consulting Agreement", fontsize=14)
    page.insert_text((72, 200), "Full name: {{text:Employee name}}", fontsize=10)
    page.insert_text((72, 240), "Sign here: {{signature}}", fontsize=10)
    page.insert_text((72, 300), "Initials: {{initials|optional}}", fontsize=10)
    page.insert_text((72, 360), "Agree: {{checkbox:Accept terms}}", fontsize=10)
    return _to_bytes(document)


def build_anchor_tag_over_widgets_pdf() -> bytes:
    """Anchor tags in a document that also has a native widget.

    The author's tags are explicit intent and must win over native widget detection.
    """
    document, page = _new_document()
    page.insert_text((72, 200), "Sign: {{signature:Authorised signatory}}", fontsize=10)
    page.add_widget(_text_widget(name="legacy_widget", rect=fitz.Rect(72, 400, 300, 424)))
    return _to_bytes(document)
