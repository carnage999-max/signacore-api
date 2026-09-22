# SignaCore PDF Form Import Context

## Objective

Fix native PDF form import so SignaCore only imports fields it can represent faithfully in its own signing experience. Do not silently turn unsupported PDF controls into generic text fields.

The immediate reference file is `/Users/Apple/Downloads/form-sample.pdf`. It is test input only; do not rely on that absolute path in automated tests or commit the file unless its licence is confirmed.

## User expectation

When a customer uploads a PDF containing a native AcroForm:

1. SignaCore should detect simple supported fields accurately and preserve their placement, dimensions, required state, and supported type.
2. Controls SignaCore cannot faithfully support must not appear as misleading text inputs.
3. The administrator must receive a clear, persistent import report explaining what was imported and what was ignored, with a path to add SignaCore fields manually.
4. No PDF JavaScript, submission action, external link, calculation, or other executable PDF behavior may run in the browser or be trusted by the server.

This is not a request to emulate Adobe Acrobat or Apple Preview. Apple Preview can operate native PDF widgets in the original document. SignaCore uses a separate browser signing UI and places its own fields over a rendered document preview.

## Reference-file evidence

`form-sample.pdf` is a 3-page, 1.5 MiB PDF. `pdfinfo` reports:

```text
Form: AcroForm
JavaScript: yes
Pages: 3
```

Rendering shows text fields, radio buttons, checkboxes, calculation fields, a hidden field, list box, dropdown, multiline field, action buttons, and page/link buttons.

Running the current `PDFEngine().analyse()` against that exact file produced 25 fields, all from `ACROFORM`:

```text
22 TEXT fields
3 CHECKBOX fields
0 SIGNATURE fields
```

Current bad conversions include:

| Original PDF control | Current SignaCore result | Correct result |
| --- | --- | --- |
| Required text input | Text input | Import if it has no unsupported semantic action. |
| Email text input with JavaScript validation | Generic text input | Do not silently import as equivalent. Skip or import only with an explicit warning that validation is unavailable, based on the approved product decision. |
| Radio group (`/Btn`, radio flags) | Three independent text fields | Skip and report unsupported radio group. |
| Checkbox | Checkbox | Import as a basic checkbox when it has no unsupported behavior. |
| Calculated fields with JavaScript | Generic text fields | Skip and report unsupported calculation/JavaScript. |
| Hidden field (`/F 6`) | Visible text input | Never import. Report hidden field ignored. |
| List box / dropdown (`/Ch`) | Generic text input | Skip and report unsupported choice field. |
| Multiline text | Single-line text input | Skip until SignaCore supports multiline text end-to-end. |
| Push, submit, reset, hide/show, page navigation, URI buttons | Generic text fields | Never import. Report unsupported action button. |
| PDF signature widget | Only supported if detected | Import as `SIGNATURE` if SignaCore can collect and flatten its own signature at that rectangle. |

The source contains `/JavaScript`, `/SubmitForm`, `/ResetForm`, `/Hide`, `/AA`, `/Btn`, `/Ch`, and external-action data. The original submit action targets `https://www.pdftron.com`.

## W-9 regression evidence

`/Users/Apple/Downloads/fw9.pdf` is the March 2024 IRS Form W-9. It is a 6-page, 138 KiB tagged PDF reported by `pdfinfo` as:

```text
Form: XFA
JavaScript: yes
Pages: 6
```

It contains 23 fallback native widgets on page 1 and none on pages 2 through 6. Running the current `PDFEngine().analyse()` against this exact file produces:

```text
23 imported fields
15 TEXT
8 CHECKBOX
0 SIGNATURE
0 required fields
```

This is not a usable import result:

- Labels are internal XFA paths such as `topmostSubform[0].Page1[0].f1_01[0]`, not customer-facing labels such as "Name" or "Address".
- The W-9's signature line is printed content, not a native signature widget. The current AcroForm-first path suppresses heuristic detection entirely, so no SignaCore signature field is created.
- Requiredness is not imported for any of the W-9 widgets, even though the form visibly identifies required information.
- The document includes XFA/JavaScript behavior that SignaCore must not execute.

There is a separate coordinate-system bug that makes imported native widgets appear in the wrong vertical location. PyMuPDF widget rectangles use a top-left origin. SignaCore stores `widget.rect.y0` unchanged, but both the signer portal and completion flattener treat the stored value as a bottom-left coordinate:

```text
W-9 first native field: y=118.0pt, height=14.0pt on a 791.968pt page
Actual visual location: 14.9% from the top
Current signer overlay location: 791.968 - 118.0 - 14.0 = 660.0pt, or 83.3% from the top
```

Relevant code:

- `services/pdf_engine.py::_extract_acroform_fields()` stores `rect.y0` directly.
- `apps/signing/static/signing/portal.js` computes overlay top as `pageData.height - field.y - field.height`.
- `services/pdf_engine.py::flatten()` makes the same bottom-origin assumption.

`DocumentField`'s effective canonical coordinate system is bottom-left origin. For newly imported PyMuPDF widgets, convert the rectangle before persistence:

```python
x = rect.x0
y = page.rect.height - rect.y1
width = rect.width
height = rect.height
```

Do not migrate or reinterpret existing persisted fields as part of this fix without a separate data-migration plan. Add regression tests proving an imported widget renders and flattens at the same visual coordinates as the source PDF.

For W-9 quality, do not expose XFA field paths as labels. Prefer a confident nearby visible label extracted from the rendered/text layer. When no reliable label is available, use a neutral, human-readable fallback such as `Page 1 field 1`; never expose internal form paths. A production-quality W-9 flow must also guide the admin to add a SignaCore signature field at the printed signature line.

## Current implementation

Important files:

- `services/pdf_engine.py`
  - `PDFEngine.analyse()` prefers AcroForm widgets whenever any are present.
  - `_extract_acroform_fields()` currently imports every widget.
  - `_map_widget_type()` only recognizes names/types containing `check`, `sig`, or `initial`; all other widgets become `TEXT`.
  - `flatten()` overlays SignaCore submissions and deletes page widgets, but does not explicitly sanitize document-level JavaScript or actions.
- `apps/documents/views.py`
  - `AdminDocumentListCreateView.post()` stores the original encrypted PDF, runs `PDFEngine.analyse()`, bulk creates `DocumentField` records, and returns `detection_summary`.
- `apps/documents/models.py`
  - `DocumentField` supports only `SIGNATURE`, `INITIALS`, `TEXT`, and `CHECKBOX`.
- `apps/signing/static/signing/portal.js`
  - `TEXT` is rendered as a single-line browser input. It does not support select controls, radio groups, multiline text, calculations, or PDF JavaScript.
- `tests/test_admin_documents.py`
  - Contains current AcroForm and heuristic upload tests.

The final signed document cannot be claimed to have active content stripped until sanitization is implemented and tested. Deleting page widgets alone is insufficient proof that document-level JavaScript or actions have been removed.

## Required behavior

### 1. Classify before importing

Replace the current blanket AcroForm import with explicit classification. Inspect each widget's native type, flags, visibility, options, and actions. A PDF with widgets must not fall back to visual heuristic detection merely because all native widgets were skipped; doing so could recreate unsupported controls from their visual outlines.

Only import a widget when its complete behavior is supported by the SignaCore signer UI and PDF flattening pipeline.

Normalize a supported native widget's rectangle into the model's existing bottom-left coordinate system before it is persisted. This applies to all AcroForm/XFA fallback widgets. Do not change the coordinate convention of existing heuristic, manual, or authored fields.

Initial safe supported set:

- Plain, visible, single-line text widgets with no calculation, validation, formatting, keystroke, focus, blur, or action scripts.
- Plain visible checkboxes with no unsupported action.
- Native signature widgets, mapped to SignaCore `SIGNATURE`, when present.
- Initials only when clearly identified and compatible with the existing SignaCore initials experience.

Do not implement a partial radio, dropdown, list-box, button, hidden-field, calculation, JavaScript, or multiline feature as part of this change.

### 2. Preserve a non-sensitive import report

Persist enough metadata for the document detail endpoint and frontend to explain an import after a refresh. Do not persist raw JavaScript, raw prefilled values, or external URL values in this report.

The report should contain safe aggregate data, for example:

```json
{
  "native_widget_count": 25,
  "imported_field_count": 5,
  "ignored_widget_count": 20,
  "warning_codes": [
    "UNSUPPORTED_RADIO_GROUP",
    "UNSUPPORTED_CHOICE_FIELD",
    "UNSUPPORTED_ACTION_BUTTON",
    "UNSUPPORTED_PDF_JAVASCRIPT",
    "HIDDEN_FIELD_IGNORED"
  ]
}
```

Choose an appropriate schema and migration. It may live on `Document` as safe import metadata or in a related model. Keep the API response backwards-compatible where possible: retain `detection_summary.source` and `detection_summary.field_count`, then extend it with the report.

### 3. User-facing frontend behavior

The organization document editor must show a concise import notice when any fields were ignored:

```text
Some PDF form controls were not imported because SignaCore does not support their behavior. Review the detected fields and add any missing fields manually.
```

The notice should name supported categories at a high level without exposing script content. It must be readable, dismissible for the current editing session, and remain available in document details after reload. Use the existing toast/design system for transient confirmation, but do not rely on a toast as the only record.

If zero supported fields were imported, the upload should still be usable as a PDF document, show the warning, and guide the administrator to add fields manually. Do not claim that it was fully detected.

### 4. Security requirements

- Never execute PDF JavaScript, launch URI actions, perform submit/reset actions, or navigate based on embedded PDF actions.
- Never expose a hidden field or its existing value to a signer.
- Treat native PDF actions as untrusted input.
- The signer page must continue to use SignaCore's own DOM controls and authenticated API flow; it must never embed a browser PDF form implementation.
- Before distributing a completed PDF, sanitize active content. The completed output must not retain AcroForm widgets, JavaScript (`/JavaScript` or `/JS`), additional actions (`/AA`), open actions, submit/reset/hide actions, URI actions, or external launch actions.
- Do not weaken encrypted-at-rest storage or organization authorization boundaries while making this change.

If reliable sanitization cannot be proven with the current PyMuPDF APIs, add a vetted PDF sanitization dependency or generate the completed document in a way that guarantees active objects are absent. Do not ship a best-effort string replacement.

## Acceptance criteria

1. A plain AcroForm text field imports as `TEXT` with the correct page, rectangle, label, and required flag.
2. A plain AcroForm checkbox imports as `CHECKBOX` with the correct rectangle.
3. A native PDF signature widget imports as `SIGNATURE`.
4. Radio groups, list boxes, combo boxes, buttons, hidden fields, multiline fields, calculated fields, and fields with JavaScript/actions are not created as `DocumentField` records.
5. A PDF with unsupported native widgets does not use heuristic detection to recreate them.
6. The upload/detail API reports imported and ignored counts plus safe warning codes.
7. The editor clearly informs the organization that unsupported controls were not imported and directs them to add fields manually.
8. A completion test inspects the saved signed PDF and proves it contains no widgets or active PDF actions/scripts.
9. Existing plain-PDF heuristic detection still works only for PDFs with no native form widgets.
10. Existing document ownership, plan gating, encrypted-file storage, and signing-flow tests continue to pass.

## Test plan

Add deterministic, repository-owned fixture builders or small fixtures under `tests/`; do not depend on the developer's Downloads directory.

At minimum, add API-level tests for:

- Supported text, checkbox, and signature AcroForm widgets.
- Required field preservation.
- Radio group ignored with `UNSUPPORTED_RADIO_GROUP` warning.
- List/combo ignored with `UNSUPPORTED_CHOICE_FIELD` warning.
- Push/submit/reset/navigation/URI action widgets ignored with `UNSUPPORTED_ACTION_BUTTON` warning.
- Hidden widget ignored and never exposed in signer context.
- Text widget with JavaScript calculation/validation ignored with `UNSUPPORTED_PDF_JAVASCRIPT` warning.
- Multiline widget ignored with `UNSUPPORTED_MULTILINE_TEXT` warning until textarea support exists.
- A mixed-form PDF imports only supported widgets and persists the report.
- A form PDF containing only unsupported widgets creates no SignaCore fields and does not invoke heuristics.
- An imported top-origin native widget is converted to the bottom-origin model position and renders at the original visual location in both the admin preview and signer portal.
- A W-9-like XFA fallback field does not expose its internal XFA path as a label.
- A W-9-like document with a printed signature line but no native signature widget tells the administrator to add a SignaCore signature field; it must not imply that a signature field was detected.
- Completed-PDF sanitization removes active form and action content.
- Regression tests for existing plain-PDF heuristic detection and signing completion.

Run formatting and the full test suite before committing:

```bash
pre-commit run --all-files
SIGNACORE_HOST_STORAGE_ROOT="$PWD/.tmp/test-media" \
SIGNACORE_HOST_STATIC_ROOT="$PWD/.tmp/test-static" \
docker compose run --rm --entrypoint "" \
  -e DB_NAME= \
  -e DEBUG=True \
  -e SECRET_KEY='test-only-signacore-secret-key-that-is-longer-than-fifty-characters-123456789' \
  -e FERNET_KEY='MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=' \
  api python manage.py test
```

Rebuild the API image first when source files have changed:

```bash
docker compose build api
```

## Delivery constraints

- Work on a dedicated feature branch and open a separate PR.
- Include backend implementation, migration if needed, frontend notice/report rendering, and tests in the same PR.
- Do not alter plan billing behavior, OAuth, storage roots, staging deployment configuration, or unrelated document-editor work.
- Use concise user-facing copy and the existing SignaCore UI style.
- Commit only after tests pass.
