"""Fingerprinting a file, so a copy of it can be recognised later.

SHA-256 of the bytes, hex, lower case. Nothing clever: the point is that anybody holding the
document can run the same command on their own machine and compare.

    shasum -a 256 agreement.pdf
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Big enough to keep the loop cheap, small enough that a large packet is never held in memory.
READ_CHUNK_BYTES = 1024 * 1024


def sha256_of_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_field_file(field_file) -> str:
    """The hash of what a stored file decrypts to, which is what a recipient would hash."""
    digest = hashlib.sha256()
    with field_file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_fingerprint(digest: str) -> str:
    """Grouped into fours, so somebody comparing two of these by eye can keep their place."""
    return " ".join(digest[index : index + 4] for index in range(0, len(digest), 4))


def sha256_of_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
