from __future__ import annotations

import fitz

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
