"""Deterministic PDF builders for form-import tests.

These exist so tests never depend on a developer's local files. Each builder returns PDF bytes
and documents the native control it is exercising.
"""

from __future__ import annotations

import fitz

PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0

FLAG_READ_ONLY = 1 << 0
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
    """Three radio kids of one group, as an exclusive choice control.

    Kids of a radio group share the field name holding the group's value; only the appearance
    state distinguishes one option from another.
    """
    document, page = _new_document()
    for index, top in enumerate((200, 230, 260)):
        page.add_widget(_text_widget(name=f"delivery_choice_{index}", rect=fitz.Rect(72, top, 86, top + 14)))
        xref = _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_RADIO | (1 << 14))
        document.xref_set_key(xref, "T", "(delivery_choice)")
    return _to_bytes(document)


def build_choice_field_pdf() -> bytes:
    """A list box and a combo box, the two shapes a /Ch choice control takes."""
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
    xref = _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_PUSHBUTTON)
    document.xref_set_key(xref, "A", f"{_action(document, '/S/ResetForm')} 0 R")

    return _to_bytes(document)


def build_unsupported_only_form_pdf() -> bytes:
    """Native widgets that are all unsupported, drawn over heuristic-looking page furniture.

    The underscores and drawn line would be picked up by heuristic detection, which must not
    run for a page that has native widgets. A submit button is the control SignaCore can never
    honour, whatever else it learns to import.
    """
    document, page = _new_document()
    page.insert_text((72, 120), "Signature:________________________ Date:____________")
    shape = page.new_shape()
    shape.draw_line((72, 320), (300, 320))
    shape.finish(width=1)
    shape.commit()

    page.add_widget(_text_widget(name="submit", rect=fitz.Rect(72, 400, 260, 424)))
    xref = _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_PUSHBUTTON)
    document.xref_set_key(xref, "A", f"{_action(document, '/S/SubmitForm/F(https://example.invalid/post)')} 0 R")

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
    """No native widgets at all, so heuristic detection is expected to run.

    The signature line carries its caption beside it, which is what says the line is for signing.
    An unnamed rule is somewhere to write, not somewhere to sign.
    """
    document, page = _new_document()
    page.insert_text((72, 120), "Name:")
    page.insert_text((72, 220), "Initials:")
    page.insert_text((72, 324), "Employee Signature", fontsize=9)
    shape = page.new_shape()
    shape.draw_line((250, 320), (460, 320))
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


def build_scanned_page_pdf() -> bytes:
    """A page whose only content is an image, as a scan or photograph produces."""
    document, page = _new_document()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 620, 800), False)
    pixmap.clear_with(235)
    page.insert_image(page.rect, pixmap=pixmap)
    return _to_bytes(document)


def build_blank_digital_pdf() -> bytes:
    """A digital page with prose but nothing that reads as a field."""
    document, page = _new_document()
    page.insert_text((72, 120), "This memorandum records the parties' shared understanding.", fontsize=11)
    page.insert_text((72, 150), "It creates no obligations and requires no response.", fontsize=11)
    return _to_bytes(document)


def _action(document: fitz.Document, body: str) -> int:
    """Create a non-JavaScript action, such as a form submission or a reset."""
    xref = document.get_new_xref()
    document.update_object(xref, f"<</Type/Action{body}>>")
    return xref


def _js_action(document: fitz.Document, script: str) -> int:
    xref = document.get_new_xref()
    document.update_object(xref, f"<</Type/Action/S/JavaScript/JS({script})>>")
    return xref


def build_formatted_text_field_pdf() -> bytes:
    """A date field carrying Adobe's format and keystroke built-ins.

    These only change how a value is displayed while it is typed, so the field is still one a
    signer fills in.
    """
    document, page = _new_document()
    page.insert_text((72, 138), "Date of birth:", fontsize=10)
    page.add_widget(_text_widget(name="dateOfBirth", rect=fitz.Rect(160, 128, 320, 146)))
    widget_xref = list(page.widgets())[-1].xref
    format_xref = _js_action(document, 'AFDate_FormatEx\\("mm/dd/yyyy"\\);')
    keystroke_xref = _js_action(document, 'AFDate_KeystrokeEx\\("mm/dd/yyyy"\\);')
    document.xref_set_key(widget_xref, "AA", f"<</F {format_xref} 0 R/K {keystroke_xref} 0 R>>")
    return _to_bytes(document)


def build_placeholder_script_pdf() -> bytes:
    """A text field whose focus and blur scripts only manage grey placeholder text."""
    document, page = _new_document()
    page.insert_text((72, 138), "Employer name:", fontsize=10)
    page.add_widget(_text_widget(name="employerName", rect=fitz.Rect(170, 128, 380, 146)))
    widget_xref = list(page.widgets())[-1].xref
    blur_xref = _js_action(document, 'event.target.value = "\\[Employer\\]";')
    focus_xref = _js_action(document, 'event.target.value = "";')
    document.xref_set_key(widget_xref, "AA", f"<</Bl {blur_xref} 0 R/Fo {focus_xref} 0 R>>")
    return _to_bytes(document)


def build_validated_text_field_pdf() -> bytes:
    """A text field with a validation rule, which decides whether a value is acceptable."""
    document, page = _new_document()
    page.add_widget(_text_widget(name="email", rect=fitz.Rect(72, 128, 320, 146)))
    widget_xref = list(page.widgets())[-1].xref
    validate_xref = _js_action(document, "if \\(!/@/.test\\(event.value\\)\\) event.rc = false;")
    document.xref_set_key(widget_xref, "AA", f"<</V {validate_xref} 0 R>>")
    return _to_bytes(document)


def build_image_button_signature_pdf() -> bytes:
    """Acrobat's signature placeholder: a push button whose action imports an image."""
    document, page = _new_document()
    page.insert_text((72, 200), "Minor's Signature:", fontsize=10)
    page.add_widget(_text_widget(name="minorSignature_af_image", rect=fitz.Rect(180, 186, 380, 216)))
    xref = _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_PUSHBUTTON)
    action_xref = _js_action(document, "event.target.buttonImportIcon\\(\\);")
    document.xref_set_key(xref, "A", f"{action_xref} 0 R")
    return _to_bytes(document)


def build_image_button_logo_pdf() -> bytes:
    """The same image placeholder on a line that is not a signature line."""
    document, page = _new_document()
    page.insert_text((72, 200), "Company logo:", fontsize=10)
    page.add_widget(_text_widget(name="companyLogo_af_image", rect=fitz.Rect(160, 186, 360, 216)))
    xref = _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_PUSHBUTTON)
    action_xref = _js_action(document, "event.target.buttonImportIcon\\(\\);")
    document.xref_set_key(xref, "A", f"{action_xref} 0 R")
    return _to_bytes(document)


def build_wrapped_label_pdf() -> bytes:
    """A signature box whose label wraps onto two lines inside a table cell."""
    document, page = _new_document()
    page.insert_text((72, 196), "Signature of Principal or", fontsize=10)
    page.insert_text((72, 210), "Authorized Official:", fontsize=10)
    page.add_widget(_text_widget(name="principalSignature_af_image", rect=fitz.Rect(230, 186, 420, 218)))
    xref = _retype_last_widget(document, page, field_type="/Btn", flags=FLAG_PUSHBUTTON)
    action_xref = _js_action(document, "event.target.buttonImportIcon\\(\\);")
    document.xref_set_key(xref, "A", f"{action_xref} 0 R")
    return _to_bytes(document)


def build_placeholder_appearance_pdf() -> bytes:
    """A form whose widget carries grey placeholder text in its own appearance.

    Generators commonly do this. Rendering the page with widgets intact bakes the placeholder
    into the preview image, so a signer ends up typing over the top of it.
    """
    document, page = _new_document()
    page.insert_text((72, 120), "Full Legal Name of Minor:", fontsize=10)
    widget = _text_widget(name="minorFullName", rect=fitz.Rect(220, 106, 470, 126))
    widget.field_value = "[Minor full legal name]"
    page.add_widget(widget)
    return _to_bytes(document)


def build_mixed_packet_pdf() -> bytes:
    """A packet whose first page is a native form and whose second was laid out to be printed.

    Assembling unrelated documents into one file is routine, and the printed page carries the
    signature block that makes the packet worth signing.
    """
    document, page = _new_document()
    page.insert_text((72, 138), "Employee name:", fontsize=10)
    page.add_widget(_text_widget(name="employeeName", rect=fitz.Rect(170, 128, 380, 146)))

    printed = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    printed.insert_text((72, 120), "Acknowledgement of Receipt", fontsize=12)
    printed.insert_text((72, 200), "Printed Legal Name:", fontsize=10)
    printed.insert_text((72, 324), "Employee Signature", fontsize=9)
    shape = printed.new_shape()
    shape.draw_line((250, 320), (470, 320))
    shape.finish(width=1)
    shape.commit()
    return _to_bytes(document)


def build_exclusive_checkbox_group_pdf() -> bytes:
    """Checkboxes made mutually exclusive by a script that switches the others off.

    Attestations are built this way when the author wants tick boxes rather than radio buttons.
    Importing the boxes separately would let a signer attest to two of them at once.
    """
    document, page = _new_document()
    page.insert_text((72, 120), "Select one status:", fontsize=10)
    names = ("CB_1", "CB_2", "CB_3")
    for index, name in enumerate(names):
        top = 140.0 + index * 20.0
        widget = fitz.Widget()
        widget.field_name = name
        widget.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
        widget.rect = fitz.Rect(72, top, 84, top + 12)
        page.add_widget(widget)
        others = "".join(f'this.getField\\("{other}"\\).value = "Off";' for other in names if other != name)
        action_xref = _js_action(document, f'if \\(this.getField\\("{name}"\\).value == "On"\\) {{{others}}}')
        document.xref_set_key(list(page.widgets())[-1].xref, "A", f"{action_xref} 0 R")
    return _to_bytes(document)


def build_printed_signature_widget_pdf() -> bytes:
    """A form built to be printed, where the signature and its date are plain text widgets.

    Government forms do this, so the widget type says nothing about what the field collects and
    the label is the only evidence. The date accompanying a signature must stay a text field.
    """
    document, page = _new_document()
    page.add_widget(
        _text_widget(name="Signature of Employee", rect=fitz.Rect(72, 300, 380, 318), label="Signature of Employee")
    )
    page.add_widget(
        _text_widget(
            name="Todays Date",
            rect=fitz.Rect(390, 300, 520, 318),
            label="Enter Today's Date of Signature mm/dd/yyyy",
        )
    )
    page.add_widget(
        _text_widget(
            name="Employee Middle Initial",
            rect=fitz.Rect(72, 340, 140, 358),
            label="Enter Middle Initial, if any",
        )
    )
    page.add_widget(_text_widget(name="Applicant Initials", rect=fitz.Rect(150, 340, 220, 358), label="Initials"))
    return _to_bytes(document)


def build_section_tooltip_pdf() -> bytes:
    """Widgets whose tooltips describe the section they sit in rather than the field itself.

    A tooltip is written for a screen reader, so it leads with where the field sits and can repeat
    across a whole section. Using it verbatim gives every field in that section the same label.
    """
    document, page = _new_document()
    descriptions = (
        "Section 2. Employer Review and Verification. Enter the issuing authority for List A.",
        "Section 2. Employer Review and Verification. Enter the document number for List A.",
    )
    names = ("Issuing Authority 1", "Document Number 1")
    for index, (name, description) in enumerate(zip(names, descriptions)):
        top = 200.0 + index * 24.0
        page.add_widget(_text_widget(name=name, rect=fitz.Rect(72, top, 380, top + 18), label=description))
    return _to_bytes(document)


def build_header_rule_pdf() -> bytes:
    """A printed page ruled off under its header and above its footer.

    Running heads are ruled this way on every page. The rules are the full width of the text block
    and look exactly like signature lines to anything measuring only shape.
    """
    document, page = _new_document()
    page.insert_text((72, 30), "Se7en Equity Holdings Inc. | Confidential", fontsize=8)
    page.insert_text((72, 400), "Reference material only.", fontsize=10)
    page.insert_text((72, 780), "Page 3 of 9", fontsize=8)
    shape = page.new_shape()
    shape.draw_line((72, 36), (540, 36))
    shape.draw_line((72, 770), (540, 770))
    shape.finish(width=1)
    shape.commit()
    return _to_bytes(document)


def build_locked_signature_pdf() -> bytes:
    """A signature line locked against typing, beside a locked field holding a fixed value.

    Forms that are invalid unless signed still mark the signature line read-only, because the
    lock is there to stop the form's own text tool writing in it.
    """
    document, page = _new_document()
    page.add_widget(
        _text_widget(
            name="Employee signature",
            rect=fitz.Rect(122, 433, 388, 449),
            label="EMPLOYEE'S SIGNATURE",
            flags=FLAG_READ_ONLY,
        )
    )
    page.add_widget(
        _text_widget(
            name="Office use only",
            rect=fitz.Rect(122, 470, 388, 486),
            label="Office use only",
            flags=FLAG_READ_ONLY,
        )
    )
    return _to_bytes(document)


def build_form_table_pdf() -> bytes:
    """A form laid out as a table beside a table that is only presenting information.

    The form names what it wants in the left cell of each row and leaves the right cell empty.
    The second table has both cells filled, which is what tells the two apart. Borders are drawn
    as separate rules, which is how a word processor emits a table.
    """
    document, page = _new_document()

    def rule(start: tuple[float, float], end: tuple[float, float]) -> None:
        # Each border is its own path, as a word processor emits them; a single path would be
        # read as one shape the size of the whole table.
        shape = page.new_shape()
        shape.draw_line(start, end)
        shape.finish(width=0.8)
        shape.commit()

    def grid(top: float, rows: int) -> None:
        for index in range(rows + 1):
            y = top + index * 24.0
            rule((60, y), (520, y))
        for x in (60, 240, 520):
            rule((x, top), (x, top + rows * 24.0))

    page.insert_text((70, 118), "Emergency contact", fontsize=11)
    for index, caption in enumerate(("Full Legal Name", "Home Address", "Mobile Phone")):
        page.insert_text((66, 130.0 + index * 24.0 + 16), caption, fontsize=9)
    grid(130.0, 3)

    page.insert_text((70, 320), "Vacation entitlement", fontsize=11)
    for index, (service, weeks) in enumerate((("1 year", "1 week"), ("2 years", "2 weeks"))):
        page.insert_text((66, 332.0 + index * 24.0 + 16), service, fontsize=9)
        page.insert_text((246, 332.0 + index * 24.0 + 16), weeks, fontsize=9)
    grid(332.0, 2)
    return _to_bytes(document)


def build_two_tables_one_page_pdf() -> bytes:
    """A form table and a reference table on one page, whose columns do not line up.

    Taking every rule on the page as one grid cuts each table at the other's column edges, and a
    two-column form comes back with a field in every part its neighbour happens to divide.
    """
    document, page = _new_document()

    def rule(start: tuple[float, float], end: tuple[float, float]) -> None:
        shape = page.new_shape()
        shape.draw_line(start, end)
        shape.finish(width=0.8)
        shape.commit()

    def grid(top: float, rows: int, columns: tuple[float, ...]) -> None:
        for index in range(rows + 1):
            y = top + index * 24.0
            rule((columns[0], y), (columns[-1], y))
        for x in columns:
            rule((x, top), (x, top + rows * 24.0))

    for index, caption in enumerate(("Legal First Name", "Legal Last Name", "Home Address")):
        page.insert_text((66, 130.0 + index * 24.0 + 16), caption, fontsize=9)
    grid(130.0, 3, (60.0, 240.0, 560.0))

    headings = ("Department", "Contact", "Extension", "Email")
    for index, row in enumerate((headings, ("Facilities", "J Bell", "318", "fac@example.test"))):
        for column, value in zip((66.0, 166.0, 296.0, 386.0), row):
            page.insert_text((column, 300.0 + index * 24.0 + 16), value, fontsize=9)
    grid(300.0, 2, (60.0, 160.0, 290.0, 380.0, 560.0))
    return _to_bytes(document)


def build_caption_above_rule_pdf() -> bytes:
    """Captions written on the line above the rule they name, spanning the same width.

    A signature block is often set out this way rather than in two columns, and a search that
    only looks to the left of a rule finds nothing to call any of them.
    """
    document, page = _new_document()
    for index, caption in enumerate(("Employee Acknowledgment Signature", "Printed Name", "Acknowledgment Date")):
        top = 200.0 + index * 52.0
        page.insert_text((60, top), caption, fontsize=10)
        shape = page.new_shape()
        shape.draw_line((60, top + 12), (500, top + 12))
        shape.finish(width=0.8)
        shape.commit()
    return _to_bytes(document)


def build_written_answer_lines_pdf() -> bytes:
    """A block of ruled lines for a written answer, under a prompt that names none of them."""
    document, page = _new_document()
    page.insert_text((60, 180), "Please explain any scheduling restrictions:", fontsize=10)
    for index in range(5):
        shape = page.new_shape()
        shape.draw_line((60, 210.0 + index * 24.0), (560, 210.0 + index * 24.0))
        shape.finish(width=0.8)
        shape.commit()
    return _to_bytes(document)


def build_optional_tick_boxes_pdf() -> bytes:
    """A question answered by one of two tick boxes, beside a line to sign.

    Requiring every box a page shows makes a document impossible to submit: a signer cannot tick
    both YES and NO. A place to sign is the one thing a printed page does speak for.
    """
    document, page = _new_document()
    page.insert_text((60, 200), "Do you require a parking permit?", fontsize=10)
    # Ruled boxes rather than a ballot-box glyph, which the built-in fonts do not carry, and which
    # is how a word processor draws a tick box anyway.
    for left, caption in ((300.0, "YES"), (380.0, "NO")):
        page.insert_text((left, 200), caption, fontsize=10)
        box = page.new_shape()
        box.draw_rect(fitz.Rect(left + 26, 191, left + 37, 202))
        box.finish(width=0.8, color=(0, 0, 0))
        box.commit()

    page.insert_text((60, 300), "Employee Signature", fontsize=10)
    rule = page.new_shape()
    rule.draw_line((250, 296), (520, 296))
    rule.finish(width=0.8)
    rule.commit()
    return _to_bytes(document)


def build_column_headed_table_pdf() -> bytes:
    """A table named by its columns, with empty rows beneath to fill in.

    The commonest shape a table takes on a paper form, and one that produced nothing while a row
    had to be part full to be read: the heading row is wholly full and every row under it is
    wholly empty, so neither qualified.
    """
    document, page = _new_document()
    page.insert_text((72, 90), "What are the main things on your plate right now?", fontsize=10)
    columns = (72.0, 250.0, 380.0, 520.0)
    rows = (110.0, 134.0, 158.0, 182.0, 206.0)

    # One path per rule, as a renderer emits them. Committed together they become a single path
    # whose bounding box is the whole table, which is nothing like a rule.
    def rule(start, end):
        shape = page.new_shape()
        shape.draw_line(start, end)
        shape.finish(width=0.8)
        shape.commit()

    for top in rows:
        for left, right in zip(columns, columns[1:]):
            rule((left, top), (right, top))
    for left in columns:
        for top, bottom in zip(rows, rows[1:]):
            rule((left, top), (left, bottom))
    for left, heading in zip(columns, ("Project", "How is it going?", "Anything blocked?")):
        page.insert_text((left + 4, 126), heading, fontsize=9)
    return _to_bytes(document)


def build_captions_under_rules_pdf() -> bytes:
    """A signature block: three rules in a row, each named by the word beneath it."""
    document, page = _new_document()
    page.insert_text((72, 120), "Please sign below to confirm these are your responses.", fontsize=10)
    for start, end in (((72, 200), (300, 200)), ((330, 200), (430, 200)), ((460, 200), (540, 200))):
        shape = page.new_shape()
        shape.draw_line(start, end)
        shape.finish(width=0.8)
        shape.commit()
    page.insert_text((72, 212), "Signature", fontsize=8)
    page.insert_text((330, 212), "Date", fontsize=8)
    page.insert_text((460, 212), "Initials", fontsize=8)
    return _to_bytes(document)


def build_running_header_pdf(pages: int = 4) -> bytes:
    """A rule under a repeated page header, which looks exactly like a writing line."""
    document = fitz.open()
    for number in range(1, pages + 1):
        page = document.new_page(width=595, height=842)
        page.insert_text((57, 64), "Se7en Equity Holdings Inc. - Employee Survey", fontsize=9)
        page.insert_text((470, 64), f"Page {number} of {pages}", fontsize=9)
        shape = page.new_shape()
        shape.draw_line((57, 76), (538, 76))
        shape.finish(width=0.8)
        shape.commit()
        # The caption stands to the left of its rule, so the rule is not underlining it.
        page.insert_text((72, 300), "Your name:", fontsize=10)
        asked = page.new_shape()
        asked.draw_line((160, 303), (420, 303))
        asked.finish(width=0.8)
        asked.commit()
    return _to_bytes(document)
