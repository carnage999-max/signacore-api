"""Removal and verification of active content in completed PDFs.

PyMuPDF's ``Document.scrub()`` is deliberately not used: it raises on documents with damaged
cross-reference entries, and on healthy documents it still leaves ``/AcroForm``, ``/AA`` and
``/JS`` in place. Removal here is explicit, and every sanitized file is re-opened and verified
so completion fails loudly rather than distributing a file with active content.
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

    catalog = document.pdf_catalog()
    for key in ACTIVE_CATALOG_KEYS:
        if key in _xref_keys(document, catalog):
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
                except Exception:  # pragma: no cover - malformed object, nothing to strip
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
            except Exception:  # pragma: no cover - unreadable object cannot carry an action
                continue
            findings.extend(
                f"object {xref} still contains {token}" for token in FORBIDDEN_TOKENS if _has_live_key(obj, token)
            )
    return findings


def _has_live_key(obj: str, token: str) -> bool:
    """A key whose value is ``null`` is equivalent to an absent key (PDF 32000-1, 7.3.9)."""
    return bool(re.search(rf"{re.escape(token)}(?!\s*null\b)", obj))


def assert_sanitized(pdf_path: str | Path) -> None:
    findings = find_active_content(pdf_path)
    if findings:
        raise PDFSanitizationError("Completed PDF still contains active content: " + "; ".join(findings[:5]))


def _is_widget_object(document: fitz.Document, xref: int) -> bool:
    try:
        return document.xref_get_key(xref, "Subtype")[1] == "/Widget"
    except Exception:  # pragma: no cover - unreadable object has no subtype
        return False


def _xref_keys(document: fitz.Document, xref: int) -> list[str]:
    try:
        return list(document.xref_get_keys(xref) or [])
    except Exception:  # pragma: no cover - unreadable object has no keys
        return []
