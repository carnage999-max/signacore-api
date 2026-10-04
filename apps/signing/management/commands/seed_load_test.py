"""Create signing requests a load generator can actually drive.

The endpoints that cost anything - page previews, submit - need the cookie issued after a signer
enters the code sent to their email. There was no way to obtain one against a running server, so
the expensive paths could not be exercised from outside at all, and a load test could only have
measured the pages that are cheap.

This mints those cookies directly, with the same signing function the OTP flow uses. It is a
command rather than a way in through the request path on purpose: running it already needs shell
access on the server, which is a position from which everything is possible anyway, so it adds no
route an attacker did not already have. An endpoint that issued sessions would.

    python manage.py seed_load_test --signers 25 --pages 8 > /tmp/signers.csv
    python manage.py seed_load_test --clean

The output is CSV, one row per signer, for the locustfile in loadtest/ to read.
"""

from __future__ import annotations

import csv
import sys
from datetime import timedelta

import fitz
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.documents.models import Document, DocumentField
from apps.signing.models import SigningRequest
from utils.otp import hash_otp
from utils.signer_session import build_signer_session_token

ORGANIZATION_NAME = "Load test workspace"
ACCOUNT_USERNAME = "load-test-owner"
TITLE_PREFIX = "Load test document"
# Decoration, so a stray row is recognisable if one is ever seen by hand. Nothing matches on
# it: the title is encrypted at rest, so it cannot be filtered on.
MARKER = "[load-test]"


class Command(BaseCommand):
    help = "Create signing requests with ready-made sessions for a load test, or remove them."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--signers", type=int, default=20, help="How many signers to create.")
        parser.add_argument(
            "--pages",
            type=int,
            default=8,
            help="Pages per document. This is the load dial: a page view is the request that costs.",
        )
        parser.add_argument("--clean", action="store_true", help="Remove everything this command created.")
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Required to run where SENTRY_ENVIRONMENT is production.",
        )

    def handle(self, *args, **options) -> None:
        self.guard_environment(allowed=options["allow_production"])

        if options["clean"]:
            self.clean()
            return

        signers = options["signers"]
        pages = options["pages"]
        if signers < 1 or pages < 1:
            raise CommandError("--signers and --pages must both be at least 1.")

        rows = self.seed(signers=signers, pages=pages)
        writer = csv.writer(self.stdout)
        writer.writerow(["token", "session_cookie", "field_id", "pages"])
        for row in rows:
            writer.writerow(row)
        self.stderr.write(f"Seeded {len(rows)} signer(s), {pages} page(s) each.")

    def guard_environment(self, *, allowed: bool) -> None:
        """Refuse where real documents live, unless told twice.

        Seeding writes documents and signing requests into whichever database it is pointed at.
        Running it against production by accident would leave fake agreements beside real ones.
        """
        environment = str(getattr(settings, "SENTRY_ENVIRONMENT", "") or "").lower()
        if environment == "production" and not allowed:
            raise CommandError(
                "This looks like production (SENTRY_ENVIRONMENT=production). "
                "Load test against staging. Pass --allow-production if you are certain."
            )

    def clean(self) -> None:
        """Find what to remove by its workspace, not by its title.

        A document's title is encrypted at rest, so matching on it compares a prefix against
        ciphertext and silently selects nothing - which is how the first version of this deleted
        no rows at all and then fell over on the protected foreign key it had left behind. The
        workspace is a plain column, and everything this command creates belongs to it.
        """
        organization = Organization.objects.filter(name=ORGANIZATION_NAME).first()
        if organization is None:
            self.stderr.write("Nothing to remove.")
            return

        documents = Document.objects.filter(organization=organization)
        removed = documents.count()
        for document in documents:
            # The stored files as well; deleting rows alone would leave the encrypted PDFs on disk.
            if document.original_pdf:
                document.original_pdf.delete(save=False)
            if document.signed_pdf:
                document.signed_pdf.delete(save=False)
        documents.delete()
        organization.delete()
        get_user_model().objects.filter(username=ACCOUNT_USERNAME).delete()
        self.stderr.write(f"Removed {removed} load test document(s).")

    @transaction.atomic
    def seed(self, *, signers: int, pages: int) -> list[tuple[str, str, str, int]]:
        owner, _ = get_user_model().objects.get_or_create(
            username=ACCOUNT_USERNAME,
            defaults={"email": "load-test@example.invalid", "is_active": False},
        )
        organization, _ = Organization.objects.get_or_create(
            name=ORGANIZATION_NAME,
            defaults={"created_by": owner},
        )

        pdf = self.build_pdf(pages)
        expires = timezone.now() + timedelta(days=int(getattr(settings, "SIGNING_LINK_EXPIRY_DAYS", 7)))
        rows: list[tuple[str, str, str, int]] = []

        for index in range(signers):
            document = Document.objects.create(
                title=f"{TITLE_PREFIX} {index + 1} {MARKER}",
                original_pdf=ContentFile(pdf, name=f"load-test-{index + 1}.pdf"),
                created_by=owner,
                organization=organization,
                status=Document.StatusEnum.SENT,
            )
            field = DocumentField.objects.create(
                document=document,
                field_type=DocumentField.FieldTypeEnum.TEXT,
                label="Full name",
                page=1,
                x=72,
                y=640,
                width=200,
                height=24,
                is_required=True,
                detection_source=DocumentField.DetectionSourceEnum.MANUAL,
                order=1,
            )
            signing_request = SigningRequest.objects.create(
                document=document,
                signer_email=f"load-test-{index + 1}@example.invalid",
                signer_name=f"Load Test Signer {index + 1}",
                expires_at=expires,
                # A session is only valid against the hash that was current when it was minted, so
                # the request needs one before the cookie can be built.
                otp_hash=hash_otp("000000"),
                otp_expires_at=expires,
            )
            cookie = build_signer_session_token(str(signing_request.id), signing_request.otp_hash)
            rows.append((str(signing_request.id), cookie, str(field.id), pages))

        return rows

    @staticmethod
    def build_pdf(pages: int) -> bytes:
        """A document that costs something to draw, without shipping a fixture.

        Rendering is the work being measured, so the pages carry enough text and ruling to be
        realistic rather than blank - a blank page rasterises far faster than a real agreement and
        would flatter the result.
        """
        document = fitz.open()
        for number in range(1, pages + 1):
            page = document.new_page(width=595, height=842)
            page.insert_text((72, 72), f"Load test document - page {number} of {pages}", fontsize=14)
            for line in range(28):
                top = 110 + line * 24
                page.insert_text(
                    (72, top), f"Clause {line + 1}. " + "The quick brown fox jumps over the lazy dog. " * 2, fontsize=9
                )
            shape = page.new_shape()
            for line in range(6):
                top = 700 + line * 20
                shape.draw_line((72, top), (520, top))
            shape.finish(width=0.6)
            shape.commit()
        payload = document.tobytes(garbage=4, deflate=True)
        document.close()
        return payload

    @property
    def stdout_is_a_terminal(self) -> bool:
        return sys.stdout.isatty()
