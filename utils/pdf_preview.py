from __future__ import annotations

import fitz

PREVIEW_DEFAULT_ZOOM = 2.0
PREVIEW_MIN_WIDTH = 60
PREVIEW_MAX_WIDTH = 1600


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
