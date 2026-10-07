"""What accumulates, and what it is safe to remove.

Nothing in SignaCore ever removed anything. Encrypted PDFs, signature images and completed copies
are written and kept, and a deleted row leaves its file behind because Django has not deleted
files on model delete since 1.3. Per-signer copies multiplied that by the number of signers.

Three jobs here, in increasing order of how much they can hurt:

1. Expired one-time codes. Their hashes sit in the database long after the view stopped accepting
   them. Clearing them loses nothing.
2. Files on disk that no row points at. A pure leak, and the only thing standing between a
   leaked file and a live one is the grace period below.
3. Documents past a retention period. This is the one that deletes something somebody wanted, so
   it does nothing at all until a retention period is set, and the default is not to have one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.documents.models import Document
from apps.signing.models import FieldSubmission, SigningRequest

logger = logging.getLogger(__name__)

# Where every stored file lives, relative to MEDIA_ROOT. Nothing outside these is ever considered.
MANAGED_DIRECTORIES = ("signacore/originals", "signacore/signed", "signacore/signatures")

# A file is written before the row that points at it is saved. Sweeping a file younger than this
# could therefore delete one that is about to be referenced, so it is left alone.
ORPHAN_GRACE_HOURS = 24


@dataclass
class CleanupReport:
    """What a run did. Counted rather than described, so it can be asserted on and logged."""

    expired_codes_cleared: int = 0
    orphaned_files_removed: int = 0
    orphaned_bytes_reclaimed: int = 0
    documents_expired: int = 0
    files_removed_with_documents: int = 0
    dry_run: bool = False
    skipped: list[str] = field(default_factory=list)

    @property
    def touched_anything(self) -> bool:
        return bool(
            self.expired_codes_cleared
            or self.orphaned_files_removed
            or self.documents_expired
            or self.files_removed_with_documents
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "expired_codes_cleared": self.expired_codes_cleared,
            "orphaned_files_removed": self.orphaned_files_removed,
            "orphaned_bytes_reclaimed": self.orphaned_bytes_reclaimed,
            "documents_expired": self.documents_expired,
            "files_removed_with_documents": self.files_removed_with_documents,
            "dry_run": self.dry_run,
            "skipped": self.skipped,
        }


def retention_days() -> int:
    """Zero, the default, means keep everything. Nothing is deleted on an assumption."""
    return max(0, int(getattr(settings, "SIGNACORE_DOCUMENT_RETENTION_DAYS", 0) or 0))


def clear_expired_codes(*, dry_run: bool = False) -> int:
    """Forget one-time codes the signing view has already stopped accepting."""
    expired = SigningRequest.objects.filter(
        otp_expires_at__lt=timezone.now(),
    ).exclude(otp_hash="", otp_expires_at__isnull=True)
    if dry_run:
        return expired.count()
    return expired.update(otp_hash="", otp_expires_at=None, updated_at=timezone.now())


def referenced_file_names() -> set[str]:
    """Every stored path any row points at, whichever field holds it."""
    names: set[str] = set()
    for queryset, fields in (
        (Document.objects.all(), ("original_pdf", "signed_pdf")),
        (SigningRequest.objects.all(), ("signed_pdf",)),
        (FieldSubmission.objects.all(), ("image_value",)),
    ):
        for row in queryset.values_list(*fields):
            names.update(value for value in row if value)
    return names


def find_orphaned_files(*, now=None) -> list[Path]:
    """Files under the managed directories that no row points at and that are old enough to sweep."""
    media_root = Path(settings.MEDIA_ROOT)
    if not media_root.exists():
        return []

    now = now or timezone.now()
    cutoff = (now - timedelta(hours=ORPHAN_GRACE_HOURS)).timestamp()
    referenced = referenced_file_names()

    orphans: list[Path] = []
    for directory in MANAGED_DIRECTORIES:
        folder = media_root / directory
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not path.is_file():
                continue
            name = f"{directory}/{path.name}"
            if name in referenced:
                continue
            if path.stat().st_mtime > cutoff:
                # Written moments ago. The row that will point at it may not be saved yet.
                continue
            orphans.append(path)
    return orphans


def remove_orphaned_files(*, dry_run: bool = False, now=None) -> tuple[int, int]:
    removed = 0
    reclaimed = 0
    for path in find_orphaned_files(now=now):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                logger.exception("Could not remove an orphaned file", extra={"path": str(path)})
                continue
        removed += 1
        reclaimed += size
    return removed, reclaimed


def documents_past_retention(*, now=None):
    """Documents whose last activity is older than the retention period.

    Measured from the most recent of creation and update rather than creation alone, so reopening
    a document to correct it starts its clock again.
    """
    days = retention_days()
    if not days:
        return Document.objects.none()
    cutoff = (now or timezone.now()) - timedelta(days=days)
    return Document.objects.filter(Q(updated_at__lt=cutoff) & Q(created_at__lt=cutoff))


def expire_documents_past_retention(*, dry_run: bool = False, now=None) -> tuple[int, int]:
    """Delete the stored files of documents past the retention period, then the documents.

    The files go first and deliberately: a row with no file is a visible fault, while a file with
    no row is invisible and would simply be swept later as an orphan.
    """
    documents = 0
    files = 0
    for document in documents_past_retention(now=now).prefetch_related("signing_requests"):
        stored = [document.original_pdf, document.signed_pdf]
        stored += [request.signed_pdf for request in document.signing_requests.all()]
        stored += [
            submission.image_value for submission in FieldSubmission.objects.filter(signing_request__document=document)
        ]
        for field_file in stored:
            if not field_file:
                continue
            if not dry_run:
                field_file.storage.delete(field_file.name)
            files += 1
        if not dry_run:
            document.delete()
        documents += 1
    return documents, files


def run_cleanup(*, dry_run: bool = False, now=None) -> CleanupReport:
    """Everything above, in one pass, reporting what it did."""
    report = CleanupReport(dry_run=dry_run)
    report.expired_codes_cleared = clear_expired_codes(dry_run=dry_run)
    report.orphaned_files_removed, report.orphaned_bytes_reclaimed = remove_orphaned_files(dry_run=dry_run, now=now)
    if retention_days():
        report.documents_expired, report.files_removed_with_documents = expire_documents_past_retention(
            dry_run=dry_run, now=now
        )
    else:
        report.skipped.append(
            "document retention: SIGNACORE_DOCUMENT_RETENTION_DAYS is not set, so nothing was deleted"
        )
    return report
