from __future__ import annotations

from hashlib import sha256

import fitz
from django.http import HttpResponse

from services.pdf_anchors import conceal_anchor_tags_on_page

PREVIEW_DEFAULT_ZOOM = 2.0
PREVIEW_MIN_WIDTH = 60
PREVIEW_MAX_WIDTH = 1600


def prepare_page_for_preview(page: fitz.Page) -> None:
    """Strip what a signer must not see from a page about to be rasterised.

    A form widget carries its own appearance, and generators commonly put grey placeholder text
    in it. Rendering the page with widgets intact bakes that placeholder into the image, so a
    signer types over the top of it. SignaCore draws its own fields, so the native widgets are
    never wanted in a preview. The stored document is untouched; this only affects the render.
    """
    conceal_anchor_tags_on_page(page)
    for widget in list(page.widgets() or []):
        page.delete_widget(widget)


def build_preview_matrix(page: fitz.Page, requested_width) -> fitz.Matrix:
    """Render at the default zoom unless a caller asks for a specific pixel width.

    Page rails request small thumbnails, and rendering those at full preview resolution would
    pull a full-size image per page.
    """
    try:
        width = int(requested_width)
    except (TypeError, ValueError):
        return fitz.Matrix(PREVIEW_DEFAULT_ZOOM, PREVIEW_DEFAULT_ZOOM)
    width = max(PREVIEW_MIN_WIDTH, min(PREVIEW_MAX_WIDTH, width))
    zoom = width / max(page.rect.width, 1.0)
    return fitz.Matrix(zoom, zoom)


def preview_etag(document, page_number: int, requested_width) -> str:
    """Name this rendered page by everything the render depends on.

    The image is a function of the stored file, the page asked for and the width asked for.
    Fields are drawn over the image by the browser rather than into it, so editing them does not
    change the page - and because fields live in their own table, saving one does not touch the
    document's ``updated_at`` either. Replacing the file does.
    """
    parts = (
        str(document.pk),
        document.original_pdf.name if document.original_pdf else "",
        document.updated_at.isoformat() if document.updated_at else "",
        str(page_number),
        str(requested_width or ""),
    )
    return '"' + sha256("|".join(parts).encode()).hexdigest()[:20] + '"'


def preview_is_unchanged(request, etag: str) -> bool:
    """Whether the caller already holds this exact image."""
    offered = request.headers.get("If-None-Match", "")
    return any(candidate.strip() == etag for candidate in offered.split(","))


def with_preview_caching(response: HttpResponse, etag: str) -> HttpResponse:
    """Let a browser re-use a page it has, without letting it skip the authorisation check.

    Rendering a page costs around half a second of CPU on the production server, and a signer
    reading a long document asks for every page, then asks again each time they scroll back. The
    expensive half of that is now avoidable: the browser offers the tag it holds and gets a 304
    with no body and no render.

    ``no-cache`` means revalidate, not "do not store". It is deliberate in place of a ``max-age``
    that would let the browser skip the request entirely. These images are the contents of someone
    else's agreement, and a signer whose session has since expired - or someone else at the same
    machine afterwards - would be served them from cache without the server being asked. The round
    trip is what keeps ``has_verified_signer_session`` in the path on every single view, and it
    costs a database query against the half-second it saves.

    ``private`` keeps the image out of any shared cache between here and the reader.
    """
    response["ETag"] = etag
    response["Cache-Control"] = "private, no-cache"
    return response
