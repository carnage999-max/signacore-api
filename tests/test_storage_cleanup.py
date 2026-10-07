"""Nothing in SignaCore removed anything, so everything accumulated. This is what removes it.

The tests that matter are the ones about what it must never touch. A cleanup that reclaims
nothing is a disappointment; one that deletes a live document is a disaster, so most of what
follows is about the second.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Organization
from apps.documents.models import Document
from apps.signing.models import SigningRequest
from services.storage_cleanup import (
    ORPHAN_GRACE_HOURS,
    clear_expired_codes,
    find_orphaned_files,
    run_cleanup,
)

from . import pdf_builders as builders

CLEANUP_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-cleanup-")


def age_file(path: Path, *, hours: int) -> None:
    """Backdate a file so the grace period no longer protects it."""
    old = (timezone.now() - timedelta(hours=hours)).timestamp()
    os.utime(path, (old, old))


@override_settings(MEDIA_ROOT=CLEANUP_MEDIA_ROOT, SIGNACORE_DOCUMENT_RETENTION_DAYS=0)
class CleanupTests(TestCase):
    def setUp(self) -> None:
        # Django rolls the database back between tests and leaves the filesystem alone, so a file
        # one test writes is still there for the next one to find and count.
        shutil.rmtree(CLEANUP_MEDIA_ROOT, ignore_errors=True)
        Path(CLEANUP_MEDIA_ROOT).mkdir(parents=True, exist_ok=True)
        self.owner = get_user_model().objects.create_user(
            username="owner", email="owner@example.com", password="pw123456", is_staff=True
        )
        self.organization = Organization.objects.create(name="Example Ltd", created_by=self.owner)
        self.document = Document.objects.create(
            title="Policy",
            original_pdf=SimpleUploadedFile("policy.pdf", builders.build_flat_pdf()),
            created_by=self.owner,
            organization=self.organization,
            status=Document.StatusEnum.DRAFT,
        )

    def media(self, *parts: str) -> Path:
        return Path(CLEANUP_MEDIA_ROOT).joinpath(*parts)

    def write_loose_file(self, name: str, *, hours_old: int) -> Path:
        path = self.media("signacore", "originals", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"leaked")
        age_file(path, hours=hours_old)
        return path

    # ------------------------------------------------------------------ what it must not touch

    def test_a_file_a_row_points_at_is_never_removed(self) -> None:
        """The whole safety case. Every other behaviour is worth less than this one."""
        stored = self.media(self.document.original_pdf.name)
        age_file(stored, hours=ORPHAN_GRACE_HOURS * 10)

        run_cleanup()

        self.assertTrue(stored.exists(), "cleanup removed a file a document still points at")
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    def test_a_file_written_moments_ago_is_left_alone(self) -> None:
        """A file is written before the row that points at it is saved. Sweeping it would race."""
        fresh = self.write_loose_file("just-written.pdf", hours_old=0)

        run_cleanup()

        self.assertTrue(fresh.exists(), "cleanup swept a file younger than the grace period")

    def test_nothing_outside_the_managed_directories_is_considered(self) -> None:
        stray = self.media("not-ours.pdf")
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"someone else's")
        age_file(stray, hours=ORPHAN_GRACE_HOURS * 10)

        run_cleanup()

        self.assertTrue(stray.exists())

    def test_no_document_is_deleted_while_no_retention_period_is_set(self) -> None:
        """The default. A deletion policy nobody chose is not one we invent."""
        Document.objects.filter(pk=self.document.pk).update(
            created_at=timezone.now() - timedelta(days=4000),
            updated_at=timezone.now() - timedelta(days=4000),
        )

        report = run_cleanup()

        self.assertEqual(report.documents_expired, 0)
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())
        self.assertTrue(any("RETENTION" in note or "retention" in note for note in report.skipped))

    def test_a_dry_run_removes_nothing(self) -> None:
        orphan = self.write_loose_file("leaked.pdf", hours_old=ORPHAN_GRACE_HOURS * 2)

        report = run_cleanup(dry_run=True)

        self.assertEqual(report.orphaned_files_removed, 1)
        self.assertTrue(orphan.exists(), "a dry run deleted a file")

    # ------------------------------------------------------------------------- what it reclaims

    def test_a_file_no_row_points_at_is_removed_once_it_is_old_enough(self) -> None:
        orphan = self.write_loose_file("leaked.pdf", hours_old=ORPHAN_GRACE_HOURS * 2)

        report = run_cleanup()

        self.assertFalse(orphan.exists())
        self.assertEqual(report.orphaned_files_removed, 1)
        self.assertEqual(report.orphaned_bytes_reclaimed, len(b"leaked"))

    def test_the_file_a_deleted_document_left_behind_is_reclaimed(self) -> None:
        """Django has not deleted files on model delete since 1.3, which is where the leak is."""
        stored = self.media(self.document.original_pdf.name)
        self.document.delete()
        age_file(stored, hours=ORPHAN_GRACE_HOURS * 2)
        self.assertTrue(stored.exists(), "the file should still be there, which is the problem")

        run_cleanup()

        self.assertFalse(stored.exists())

    def test_an_expired_code_is_forgotten(self) -> None:
        signing_request = SigningRequest.objects.create(
            document=self.document,
            signer_name="Avery",
            signer_email="avery@example.com",
            otp_hash="a-hash-that-no-longer-opens-anything",
            otp_expires_at=timezone.now() - timedelta(hours=2),
        )

        cleared = clear_expired_codes()

        signing_request.refresh_from_db()
        self.assertEqual(cleared, 1)
        self.assertEqual(signing_request.otp_hash, "")
        self.assertIsNone(signing_request.otp_expires_at)

    def test_a_code_that_has_not_expired_is_kept(self) -> None:
        signing_request = SigningRequest.objects.create(
            document=self.document,
            signer_name="Avery",
            signer_email="avery@example.com",
            otp_hash="still-valid",
            otp_expires_at=timezone.now() + timedelta(minutes=5),
        )

        clear_expired_codes()

        signing_request.refresh_from_db()
        self.assertEqual(signing_request.otp_hash, "still-valid")

    # ---------------------------------------------------------------------- retention, when set

    @override_settings(SIGNACORE_DOCUMENT_RETENTION_DAYS=30)
    def test_a_document_past_retention_goes_with_its_files(self) -> None:
        stored = self.media(self.document.original_pdf.name)
        Document.objects.filter(pk=self.document.pk).update(
            created_at=timezone.now() - timedelta(days=60),
            updated_at=timezone.now() - timedelta(days=60),
        )

        report = run_cleanup()

        self.assertEqual(report.documents_expired, 1)
        self.assertFalse(Document.objects.filter(pk=self.document.pk).exists())
        self.assertFalse(stored.exists(), "the row went but its encrypted file stayed")

    @override_settings(SIGNACORE_DOCUMENT_RETENTION_DAYS=30)
    def test_a_document_touched_recently_is_kept_however_old_it_is(self) -> None:
        """Reopening a document to correct it starts its clock again."""
        Document.objects.filter(pk=self.document.pk).update(
            created_at=timezone.now() - timedelta(days=400),
            updated_at=timezone.now() - timedelta(days=1),
        )

        report = run_cleanup()

        self.assertEqual(report.documents_expired, 0)
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    @override_settings(SIGNACORE_DOCUMENT_RETENTION_DAYS=30)
    def test_a_dry_run_deletes_no_document_either(self) -> None:
        Document.objects.filter(pk=self.document.pk).update(
            created_at=timezone.now() - timedelta(days=60),
            updated_at=timezone.now() - timedelta(days=60),
        )

        report = run_cleanup(dry_run=True)

        self.assertEqual(report.documents_expired, 1)
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    # ------------------------------------------------------------------------------- the command

    def test_the_command_says_it_will_delete_nothing_when_no_retention_is_set(self) -> None:
        out = StringIO()
        call_command("cleanup", "--dry-run", stdout=out, stderr=StringIO())

        printed = out.getvalue()
        self.assertIn("SIGNACORE_DOCUMENT_RETENTION_DAYS is not set", printed)
        self.assertIn("Would remove", printed)

    def test_the_command_removes_what_it_found_when_not_a_dry_run(self) -> None:
        orphan = self.write_loose_file("leaked.pdf", hours_old=ORPHAN_GRACE_HOURS * 2)
        out = StringIO()

        call_command("cleanup", stdout=out, stderr=StringIO())

        self.assertFalse(orphan.exists())
        self.assertIn("Removed", out.getvalue())


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="signacore-cleanup-empty-"))
class CleanupWithNothingToDoTests(TestCase):
    def test_a_run_against_an_empty_installation_is_not_an_error(self) -> None:
        report = run_cleanup()

        self.assertFalse(report.touched_anything)
        self.assertEqual(find_orphaned_files(), [])
