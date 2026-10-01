"""Removal and verification of active content in completed PDFs.

PyMuPDF's ``Document.scrub()`` is deliberately not used: it raises on documents with damaged
cross-reference entries, and on healthy documents it still leaves ``/AcroForm``, ``/AA`` and
``/JS`` in place. Removal here is explicit, and every sanitized file is re-opened and verified
so completion fails loudly rather than distributing a file with active content.

The split of responsibility between the two halves is what makes that claim hold. Removal is
best effort: a document can be damaged in ways that defeat an individual edit, and refusing to
strip anything because one object resisted would help nobody. Verification is total. It is the
verifier, not the remover, that decides whether a file may be distributed, so it treats an object
it cannot read as a failure rather than assuming an object nobody can parse must be inert. An
unreadable object is precisely the one whose contents are unknown.

That is affordable because the verifier only ever inspects a file this module has just written
with ``garbage=4, clean=True``, which rebuilds the cross-reference table. Source documents do
carry dangling references - the IRS W-9 has two - and none of them survive into the output.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz

ACTIVE_OBJECT_KEYS = ("JS", "JavaScript", "AA", "OpenAction", "A")
ACTIVE_CATALOG_KEYS = ("AcroForm", "OpenAction", "AA", "Names", "Perms", "XFA")
FORBIDDEN_TOKENS = (
    "/AA",
    "/AcroForm",
    "/GoToR",
    "/Hide",
    "/ImportData",
    "/JS",
    "/JavaScript",
    "/Launch",
    "/OpenAction",
    "/ResetForm",
    "/SubmitForm",
    "/URI",
    "/Widget",
    "/XFA",
)


class PDFSanitizationError(RuntimeError):
    """Raised when active content could not be removed from a completed document."""


def sanitize_document(document: fitz.Document) -> None:
    """Strip form widgets, links, scripts, and actions from an open document in place."""
    for page in document:
        for widget in list(page.widgets() or []):
            page.delete_widget(widget)
        for link in list(page.get_links() or []):
            page.delete_link(link)

    # Set unconditionally rather than only where the key was read back. A null value is equivalent
    # to an absent key (PDF 32000-1, 7.3.9), so writing one that was never there costs nothing,
    # while reading the catalog first would mean a catalog that resisted inspection kept every
    # action it had.
    catalog = document.pdf_catalog()
    for key in ACTIVE_CATALOG_KEYS:
        document.xref_set_key(catalog, key, "null")

    for xref in range(1, document.xref_length()):
        keys = _xref_keys(document, xref)
        if not keys:
            continue
        if _is_widget_object(document, xref):
            document.update_object(xref, "<<>>")
            continue
        for key in ACTIVE_OBJECT_KEYS:
            if key in keys:
                try:
                    document.xref_set_key(xref, key, "null")
                except Exception:  # pragma: no cover - the verifier decides whether this mattered
                    continue


def find_active_content(pdf_path: str | Path) -> list[str]:
    """Return human-readable findings for any active content left in a saved PDF."""
    findings: list[str] = []
    with fitz.open(pdf_path) as document:
        if document.is_form_pdf:
            findings.append("document still reports an interactive form")
        for page_number, page in enumerate(document, start=1):
            if list(page.widgets() or []):
                findings.append(f"page {page_number} still has form widgets")
            if page.get_links():
                findings.append(f"page {page_number} still has link actions")
        for xref in range(1, document.xref_length()):
            try:
                obj = document.xref_object(xref, compressed=True)
            except Exception as error:
                findings.append(f"object {xref} could not be read to verify it: {error}")
                continue
            if not isinstance(obj, str):
                findings.append(f"object {xref} could not be read to verify it")
                continue
            findings.extend(
                f"object {xref} still contains {token}" for token in FORBIDDEN_TOKENS if _has_live_key(obj, token)
            )
    return findings


def _has_live_key(obj: str, token: str) -> bool:
    """Check for a key, not for text that merely starts the same way.

    A name runs until a delimiter or whitespace (PDF 32000-1, 7.3.5), so ``/AA`` followed by
    another regular character is the start of a longer name and not the additional-actions key.
    Fonts are routinely subset under a six-letter prefix - ``/FontName/AAAAAA+DejaVuSans`` is the
    usual shape - and searching for the bare token found ``/AA`` inside every one of them, which
    condemned a perfectly inert document as carrying active content.

    A key whose value is ``null`` is equivalent to an absent key (7.3.9).
    """
    return bool(re.search(rf"{re.escape(token)}(?=[\s/\[\]<>(){{}}%]|$)(?!\s*null\b)", obj))


def assert_sanitized(pdf_path: str | Path) -> None:
    findings = find_active_content(pdf_path)
    if findings:
        raise PDFSanitizationError("Completed PDF still contains active content: " + "; ".join(findings[:5]))


def _is_widget_object(document: fitz.Document, xref: int) -> bool:
    """Whether this object is a form widget. Both of the following leave removal best effort."""
    try:
        return document.xref_get_key(xref, "Subtype")[1] == "/Widget"
    except Exception:  # pragma: no cover - a widget left behind is a finding, not a silent pass
        return False


def _xref_keys(document: fitz.Document, xref: int) -> list[str]:
    try:
        return list(document.xref_get_keys(xref) or [])
    except Exception:  # pragma: no cover - anything left in an object it skips is a finding
        return []
