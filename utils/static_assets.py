"""Cache-busting URLs for the signer portal's own stylesheet and script.

The portal is served by Django as a template rather than by the frontend build, so its assets have
no content hash in their names. A browser that has seen ``/static/signing/portal.css`` once keeps
serving its copy, and so does anything caching in front of it, which means a deployed change to the
portal does not reach anyone already using it until their cache happens to expire.

Appending a digest of the file's contents gives each version its own URL without changing how the
files are stored or served.
"""

from __future__ import annotations

import logging
from hashlib import sha256
from pathlib import Path

from django.contrib.staticfiles import finders
from django.templatetags.static import static

logger = logging.getLogger(__name__)

_digests: dict[str, str] = {}


def versioned_static(path: str) -> str:
    """Return the static URL for ``path`` with a digest of its contents attached."""
    url = static(path)
    digest = _digest_for(path)
    if not digest:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={digest}"


def _digest_for(path: str) -> str:
    """Digest the file once per process; its contents cannot change without a new deploy."""
    if path in _digests:
        return _digests[path]

    located = finders.find(path)
    if not located:
        # Nothing to version. The URL still works, so this is not worth failing a page render for.
        logger.warning("Static asset not found for versioning", extra={"path": path})
        _digests[path] = ""
        return ""

    digest = sha256(Path(located).read_bytes()).hexdigest()[:12]
    _digests[path] = digest
    return digest
