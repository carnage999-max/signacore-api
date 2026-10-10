"""The certificate that travels with a signed document.

A signed PDF shows what was agreed. It does not show who agreed, when, from where, or that the
file in front of you is the one the signature was made against. These tests cover the pages
that do, and in particular that every fact on them came from what was recorded at the time.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta

import fitz
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import AccountProfile, Organization, OrganizationMembership
from apps.documents.models import Document, DocumentField
from apps.signing.models import SigningRequest
from utils.file_storage import temporary_plaintext_file
from utils.fingerprint import format_fingerprint, sha256_of_bytes, sha256_of_path

from .test_admin_documents import build_flat_pdf

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-certificate-media-")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_TEST_OTP_CODE="123456",
)
class CompletionCertificateTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="cert-admin", email="owner@example.com", password="password123", is_staff=True
        )
        AccountProfile.objects.create(
            user=self.user,
            account_type=AccountProfile.AccountTypeEnum.COMPANY,
            email="owner@example.com",
            display_name="Priya Shah",
        )
        self.organization = Organization.objects.create(name="Certificate Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        self.pdf_bytes = build_flat_pdf()
        self.document = Document.objects.create(
            title="Contractor Agreement",
            original_pdf=SimpleUploadedFile("deal.pdf", self.pdf_bytes, content_type="application/pdf"),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
            original_sha256=sha256_of_bytes(self.pdf_bytes),
        )
        self.field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Full legal name",
            page=1,
            x=72,
            y=620,
            width=180,
            height=24,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )
        self.signing_request = SigningRequest.objects.create(
            document=self.document,
            signer_email="avery@northwind.studio",
            signer_name="Avery Morgan",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def sign(self, ip: str = "81.2.44.10") -> None:
        signer = APIClient()
        signer.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP=ip)
        signer.post(f"/api/sign/{self.signing_request.id}/otp/send/", HTTP_X_REAL_IP=ip)
        verify = signer.post(
            f"/api/sign/{self.signing_request.id}/otp/verify/", {"otp": "123456"}, format="json", HTTP_X_REAL_IP=ip
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = signer.post(
                f"/api/sign/{self.signing_request.id}/submit/",
                {
                    "session_token": verify.json()["session_token"],
                    f"field_{self.field.id}_type": "TEXT",
                    f"field_{self.field.id}_value": "Avery Morgan",
                },
                format="multipart",
                HTTP_X_REAL_IP=ip,
            )
        self.assertEqual(response.status_code, 200, response.content)

    def signed_text(self) -> str:
        self.signing_request.refresh_from_db()
        with temporary_plaintext_file(self.signing_request.signed_pdf, suffix=".pdf") as path:
            with fitz.open(path) as pdf:
                return "\n".join(page.get_text() for page in pdf)

    # ------------------------------------------------------------- it travels with the document

    def test_the_certificate_is_part_of_the_copy_the_signer_receives(self) -> None:
        """Appended rather than delivered separately, so the two cannot be parted later."""
        self.sign()

        self.signing_request.refresh_from_db()
        with temporary_plaintext_file(self.signing_request.signed_pdf, suffix=".pdf") as path:
            with fitz.open(path) as pdf:
                self.assertGreater(pdf.page_count, 1, "the certificate should add pages")
        self.assertIn("Certificate of Completion", self.signed_text())

    def test_it_names_who_signed_and_how_they_were_identified(self) -> None:
        self.sign()

        text = self.signed_text()
        self.assertIn("Avery Morgan", text)
        self.assertIn("avery@northwind.studio", text)
        self.assertIn("one-time code", text)

    def test_it_carries_the_whole_trail(self) -> None:
        self.sign()

        text = self.signed_text()
        for label in ("Document opened", "Email verified", "Signed and submitted"):
            self.assertIn(label, text)

    def test_it_records_where_each_step_came_from(self) -> None:
        self.sign(ip="203.0.113.7")

        self.assertIn("203.0.113.7", self.signed_text())

    def test_every_time_says_which_zone_it_is_in(self) -> None:
        """A timestamp without a zone is the commonest way an audit record becomes useless."""
        self.sign()

        self.assertIn("UTC", self.signed_text())

    # ------------------------------------------------------------------------- the fingerprint

    def test_it_prints_the_fingerprint_of_the_document_as_uploaded(self) -> None:
        self.sign()

        # Grouped into fours on the page, so it is compared in that form.
        self.assertIn(format_fingerprint(sha256_of_bytes(self.pdf_bytes))[:39], self.signed_text())

    def test_the_fingerprint_is_of_the_original_rather_than_of_the_signed_copy(self) -> None:
        """A document cannot state its own hash, and the original is what a holder can check."""
        self.sign()

        self.signing_request.refresh_from_db()
        with temporary_plaintext_file(self.signing_request.signed_pdf, suffix=".pdf") as path:
            delivered = sha256_of_path(path)
        self.assertNotEqual(delivered, self.document.original_sha256)
        self.assertNotIn(format_fingerprint(delivered)[:39], self.signed_text())

    def test_the_delivered_copy_is_fingerprinted_too(self) -> None:
        """Kept beside the file rather than inside it, so our archive copy can be shown intact."""
        self.sign()

        self.signing_request.refresh_from_db()
        with temporary_plaintext_file(self.signing_request.signed_pdf, suffix=".pdf") as path:
            self.assertEqual(self.signing_request.signed_pdf_sha256, sha256_of_path(path))

    def test_a_document_uploaded_before_fingerprinting_says_so(self) -> None:
        """Rather than printing nothing, or an empty value that reads as a fingerprint."""
        self.document.original_sha256 = ""
        self.document.save(update_fields=["original_sha256"])

        self.sign()

        self.assertIn("Not recorded", self.signed_text())

    # ------------------------------------------------------------------------------ and it is safe

    def test_the_certificate_does_not_reintroduce_active_content(self) -> None:
        """Flatten sanitises what it produces; these pages are added after that."""
        from services.pdf_sanitizer import find_active_content

        self.sign()

        self.signing_request.refresh_from_db()
        with temporary_plaintext_file(self.signing_request.signed_pdf, suffix=".pdf") as path:
            self.assertEqual(find_active_content(path), [])

    def test_the_agreement_itself_is_still_the_first_page(self) -> None:
        """The certificate is an appendix. Nobody should open their contract on a cover sheet."""
        self.sign()

        self.signing_request.refresh_from_db()
        with temporary_plaintext_file(self.signing_request.signed_pdf, suffix=".pdf") as path:
            with fitz.open(path) as pdf:
                first = pdf[0].get_text()
        self.assertNotIn("Certificate of Completion", first)
        self.assertIn("Avery Morgan", first)

    def test_it_does_not_promise_the_agreement_is_enforceable(self) -> None:
        """We record what we observed. Enforceability is for the document, the parties and law."""
        self.sign()

        text = self.signed_text()
        self.assertIn("depends on the document, the parties and the law", text)

    # ------------------------------------------------------------------------------- the seal

    def seal_words(self, pdf) -> list[tuple]:
        """Words drawn inside the seal on the first certificate page."""
        page = pdf[self.certificate_start(pdf)]
        return [word for word in page.get_text("words") if word[3] < 140 and word[0] > page.rect.width / 2]

    def certificate_start(self, pdf) -> int:
        for index, page in enumerate(pdf):
            if "Certificate of Completion" in page.get_text():
                return index
        raise AssertionError("no certificate page")

    def test_the_seal_says_how_many_signatures_were_collected(self) -> None:
        self.sign()

        self.assertIn("1 SIGNATURE", self.signed_text())
        self.assertIn("EMAIL VERIFIED", self.signed_text())

    def test_every_word_of_the_seal_stays_inside_its_ring(self) -> None:
        """The ring narrows quickly, and PyMuPDF drops text that will not fit without saying so.

        This asserts the geometry rather than trusting that the text turned up somewhere.
        """
        from services.completion_certificate import MARGIN, SEAL_RADIUS

        self.sign()
        self.signing_request.refresh_from_db()
        with temporary_plaintext_file(self.signing_request.signed_pdf, suffix=".pdf") as path:
            with fitz.open(path) as pdf:
                page = pdf[self.certificate_start(pdf)]
                centre_x = page.rect.width - MARGIN - SEAL_RADIUS
                centre_y = MARGIN + SEAL_RADIUS
                inner = SEAL_RADIUS - 6
                # Anything drawn in the square the seal occupies, so a word that has spilled out
                # of the ring is still caught rather than filtered away as somebody else's.
                box = fitz.Rect(
                    centre_x - SEAL_RADIUS * 2,
                    centre_y - SEAL_RADIUS * 2,
                    centre_x + SEAL_RADIUS * 2,
                    centre_y + SEAL_RADIUS * 2,
                )
                found = []
                for x0, y0, x1, y1, word, *_ in page.get_text("words"):
                    if not box.intersects(fitz.Rect(x0, y0, x1, y1)):
                        continue
                    found.append(word)
                    for corner_x, corner_y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                        distance = ((corner_x - centre_x) ** 2 + (corner_y - centre_y) ** 2) ** 0.5
                        self.assertLess(distance, inner, f"'{word}' spills out of the seal")

        self.assertEqual(found, ["1", "SIGNATURE", "EMAIL", "VERIFIED"])

    def test_the_seal_claims_nothing_it_cannot_back_up(self) -> None:
        """It is our own mark. Borrowing a compliance badge we do not hold would be worse than
        having no seal at all."""
        self.sign()
        text = self.signed_text()

        for borrowed in ("SOC 2", "HIPAA", "ISO", "PCI", "FDA", "eIDAS", "certified", "Certified"):
            self.assertNotIn(borrowed, text)
