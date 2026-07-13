from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from datetime import timedelta

import fitz
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.documents.models import Document, DocumentField
from apps.signing.models import FieldSubmission, SigningRequest
from utils.otp import generate_otp

from .test_admin_documents import build_flat_pdf


TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-test-media-")


def build_png_pixel() -> bytes:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
    pixmap.clear_with(0x000000)
    return pixmap.tobytes("png")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class SignerFlowTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin",
            password="password123",
        )
        self.document = Document.objects.create(
            title="Employment Offer",
            original_pdf=SimpleUploadedFile(
                "offer.pdf",
                build_flat_pdf(),
                content_type="application/pdf",
            ),
            created_by=self.user,
            status=Document.StatusEnum.SENT,
        )
        self.text_field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Employee Name",
            page=1,
            x=72,
            y=620,
            width=180,
            height=24,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )
        self.signature_field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.SIGNATURE,
            label="Employee Signature",
            page=1,
            x=72,
            y=500,
            width=180,
            height=36,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=2,
        )
        self.signing_request = SigningRequest.objects.create(
            document=self.document,
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_signer_context_returns_document_fields(self) -> None:
        response = self.client.get(f"/api/sign/{self.signing_request.id}/")

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["document_title"], "Employment Offer")
        self.assertEqual(payload["signer_name"], "Jane Doe")
        self.assertEqual(payload["status"], "PENDING")
        self.assertEqual(len(payload["fields"]), 2)
        self.assertEqual(payload["page_count"], 1)
        self.assertEqual(len(payload["pages"]), 1)

    def test_signer_portal_page_renders(self) -> None:
        response = self.client.get(f"/sign/{self.signing_request.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Signacore Signer Portal")

    def test_signer_preview_endpoint_returns_png(self) -> None:
        response = self.client.get(f"/api/sign/{self.signing_request.id}/pages/1/preview/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_send_otp_masks_email_and_persists_hash(self) -> None:
        mail.outbox = []
        response = self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["masked_email"], "j***@example.com")
        self.signing_request.refresh_from_db()
        self.assertIsNotNone(self.signing_request.otp_hash)
        self.assertIsNotNone(self.signing_request.otp_expires_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("OTP code", mail.outbox[0].body)

    def test_verify_otp_returns_submit_session(self) -> None:
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            response = self.client.post(
                f"/api/sign/{self.signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.10, 10.0.0.2",
            )

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertIn("session_token", payload)
        self.signing_request.refresh_from_db()
        self.assertEqual(self.signing_request.status, SigningRequest.StatusEnum.OTP_VERIFIED)
        self.assertEqual(self.signing_request.ip_address, "203.0.113.10")

    def test_submit_requires_all_required_fields(self) -> None:
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            verify_response = self.client.post(
                f"/api/sign/{self.signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
            )
        session_token = verify_response.json()["session_token"]

        response = self.client.post(
            f"/api/sign/{self.signing_request.id}/submit/",
            {
                "session_token": session_token,
                f"field_{self.text_field.id}_type": "TEXT",
                f"field_{self.text_field.id}_value": "Jane Doe",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn(str(self.signature_field.id), response.json()["field_errors"])

    def test_submit_requires_required_checkbox_field_to_be_checked(self) -> None:
        checkbox_field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.CHECKBOX,
            label="Email notices consent",
            page=1,
            x=72,
            y=460,
            width=12,
            height=12,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=3,
        )

        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            verify_response = self.client.post(
                f"/api/sign/{self.signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
            )
        session_token = verify_response.json()["session_token"]

        response = self.client.post(
            f"/api/sign/{self.signing_request.id}/submit/",
            {
                "session_token": session_token,
                f"field_{self.text_field.id}_type": "TEXT",
                f"field_{self.text_field.id}_value": "Jane Doe",
                f"field_{self.signature_field.id}_type": "SIGNATURE_PNG",
                f"field_{self.signature_field.id}_image": SimpleUploadedFile(
                    "signature.png",
                    build_png_pixel(),
                    content_type="image/png",
                ),
                f"field_{checkbox_field.id}_type": "CHECKBOX",
                f"field_{checkbox_field.id}_checked": "false",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn(str(checkbox_field.id), response.json()["field_errors"])

    def test_submit_completes_single_signer_document_and_generates_signed_pdf(self) -> None:
        mail.outbox = []
        checkbox_field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.CHECKBOX,
            label="Email notices consent",
            page=1,
            x=72,
            y=460,
            width=12,
            height=12,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=3,
        )
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            verify_response = self.client.post(
                f"/api/sign/{self.signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
            )
        session_token = verify_response.json()["session_token"]

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/sign/{self.signing_request.id}/submit/",
                {
                    "session_token": session_token,
                    f"field_{self.text_field.id}_type": "TEXT",
                    f"field_{self.text_field.id}_value": "Jane Doe",
                    f"field_{self.signature_field.id}_type": "SIGNATURE_PNG",
                    f"field_{self.signature_field.id}_image": SimpleUploadedFile(
                        "signature.png",
                        build_png_pixel(),
                        content_type="image/png",
                    ),
                    f"field_{checkbox_field.id}_type": "CHECKBOX",
                    f"field_{checkbox_field.id}_checked": "true",
                },
                format="multipart",
                HTTP_X_FORWARDED_FOR="198.51.100.5, 10.0.0.2",
            )

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["status"], "COMPLETED")
        self.signing_request.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(self.signing_request.status, SigningRequest.StatusEnum.SIGNED)
        self.assertEqual(self.signing_request.ip_address, "198.51.100.5")
        self.assertEqual(self.document.status, Document.StatusEnum.COMPLETED)
        self.assertTrue(self.document.signed_pdf.name.endswith(".pdf"))
        self.assertEqual(FieldSubmission.objects.count(), 3)
        self.assertGreaterEqual(len(mail.outbox), 2)
