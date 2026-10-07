"""Run the cleanup by hand, which is how it should be met for the first time.

``--dry-run`` reports what would go without touching anything, and is the only sensible way to
find out what a retention period you are considering would actually delete.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from services.storage_cleanup import ORPHAN_GRACE_HOURS, retention_days, run_cleanup


class Command(BaseCommand):
    help = "Remove expired one-time codes, files no row points at, and documents past retention."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be removed without removing any of it.",
        )

    def handle(self, *args, **options) -> None:
        dry_run = bool(options["dry_run"])
        days = retention_days()

        self.stdout.write(f"Orphaned files are swept once they are {ORPHAN_GRACE_HOURS} hours old.")
        if days:
            self.stdout.write(f"Documents are deleted {days} days after their last activity.")
        else:
            self.stdout.write("SIGNACORE_DOCUMENT_RETENTION_DAYS is not set, so no document will be deleted.")

        report = run_cleanup(dry_run=dry_run)
        prefix = "Would remove" if dry_run else "Removed"

        self.stdout.write("")
        self.stdout.write(f"{prefix}: {report.expired_codes_cleared} expired one-time code(s)")
        self.stdout.write(
            f"{prefix}: {report.orphaned_files_removed} orphaned file(s), "
            f"{report.orphaned_bytes_reclaimed / (1024 * 1024):.1f} MB"
        )
        self.stdout.write(
            f"{prefix}: {report.documents_expired} document(s) past retention, "
            f"with {report.files_removed_with_documents} stored file(s)"
        )
        for note in report.skipped:
            self.stdout.write(self.style.WARNING(f"Skipped {note}"))

        if dry_run and report.touched_anything:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Nothing was removed. Run again without --dry-run."))
