from __future__ import annotations

import tempfile
from pathlib import Path
from datetime import timedelta

import fitz
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.documents.models import Document, DocumentField
from apps.signing.models import SigningRequest


def build_acroform_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=612, height=792)

    text_widget = fitz.Widget()
    text_widget.field_name = "employee_name"
    text_widget.field_label = "Employee Name"
    text_widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    text_widget.rect = fitz.Rect(72, 144, 240, 168)
    text_widget.field_flags = 1 << 1
    page.add_widget(text_widget)

    signature_widget = fitz.Widget()
    signature_widget.field_name = "employee_signature"
    signature_widget.field_label = "Employee Signature"
    signature_widget.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
    signature_widget.rect = fitz.Rect(72, 220, 280, 260)
    signature_widget.field_flags = 1 << 1
    page.add_widget(signature_widget)

    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


def build_flat_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 120), "Name:")
    page.insert_text((72, 220), "Initials:")
    shape = page.new_shape()
    shape.draw_line((72, 320), (240, 320))
    shape.finish(width=1)
    shape.commit()

    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
)
class AdminDocumentUploadTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin",
            password="password123",
        )
        self.client.credentials(HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET)

    def test_admin_routes_require_signacore_secret_header(self) -> None:
        self.client.credentials()

        response = self.client.get("/api/admin/documents/")

        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(response.json()["detail"], "Invalid Signacore secret.")

        self.client.credentials(HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET)

    def test_upload_pdf_creates_document_and_extracts_acroform_fields(self) -> None:
        upload = SimpleUploadedFile(
            "employment.pdf",
            build_acroform_pdf(),
            content_type="application/pdf",
        )

        response = self.client.post(
            "/api/admin/documents/",
            {"title": "Employment Agreement", "pdf_file": upload},
            format="multipart",
        )

        self.assertEqual(response.status_code, 201, response.json())
        payload = response.json()
        self.assertEqual(payload["title"], "Employment Agreement")
        self.assertEqual(payload["status"], "DRAFT")
        self.assertEqual(payload["page_count"], 1)
        self.assertEqual(payload["detection_summary"], {"source": "ACROFORM", "field_count": 2})
        self.assertEqual(len(payload["fields"]), 2)
        self.assertEqual(
            [field["field_type"] for field in payload["fields"]],
            ["TEXT", "SIGNATURE"],
        )

        document = Document.objects.get(pk=payload["id"])
        self.assertEqual(document.created_by.username, settings.SIGNACORE_SERVICE_USERNAME)
        self.assertEqual(document.fields.count(), 2)

    def test_upload_pdf_falls_back_to_heuristic_detection_when_no_widgets_exist(self) -> None:
        upload = SimpleUploadedFile(
            "flat-contract.pdf",
            build_flat_pdf(),
            content_type="application/pdf",
        )

        response = self.client.post(
            "/api/admin/documents/",
            {"title": "Flat Contract", "pdf_file": upload},
            format="multipart",
        )

        self.assertEqual(response.status_code, 201, response.json())
        payload = response.json()
        self.assertEqual(payload["detection_summary"]["source"], "HEURISTIC")
        self.assertGreaterEqual(payload["detection_summary"]["field_count"], 2)
        self.assertTrue(
            any(field["field_type"] == "SIGNATURE" for field in payload["fields"])
        )
        self.assertTrue(
            any(field["field_type"] == "TEXT" for field in payload["fields"])
        )

    def test_list_documents_returns_signer_progress_counts(self) -> None:
        first = Document.objects.create(
            title="One",
            original_pdf=SimpleUploadedFile("one.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )
        second = Document.objects.create(
            title="Two",
            original_pdf=SimpleUploadedFile("two.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            status=Document.StatusEnum.SENT,
        )
        DocumentField.objects.create(
            document=second,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Name",
            page=1,
            x=10,
            y=10,
            width=100,
            height=20,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )

        response = self.client.get("/api/admin/documents/")

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(len(payload["items"]), 2)
        returned_ids = {item["id"] for item in payload["items"]}
        self.assertEqual(returned_ids, {str(first.id), str(second.id)})

    def test_document_detail_returns_fields(self) -> None:
        document = Document.objects.create(
            title="Offer Letter",
            original_pdf=SimpleUploadedFile("offer.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )
        field = DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Employee Name",
            page=1,
            x=20,
            y=40,
            width=120,
            height=20,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )

        response = self.client.get(f"/api/admin/documents/{document.id}/")

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["id"], str(document.id))
        self.assertEqual(len(payload["fields"]), 1)
        self.assertEqual(payload["fields"][0]["id"], str(field.id))

    def test_create_manual_field_persists_to_document(self) -> None:
        document = Document.objects.create(
            title="NDA",
            original_pdf=SimpleUploadedFile("nda.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )

        response = self.client.post(
            f"/api/admin/documents/{document.id}/fields/",
            {
                "field_type": "INITIALS",
                "label": "Employee Initials",
                "page": 1,
                "x": 72,
                "y": 120,
                "width": 48,
                "height": 24,
                "is_required": True,
                "order": 1,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.json())
        payload = response.json()
        self.assertEqual(payload["field_type"], "INITIALS")
        self.assertEqual(payload["detection_source"], "MANUAL")
        self.assertEqual(document.fields.count(), 1)

    def test_patch_field_updates_coordinates_and_required_flag(self) -> None:
        document = Document.objects.create(
            title="Agreement",
            original_pdf=SimpleUploadedFile("agreement.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )
        field = DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Name",
            page=1,
            x=20,
            y=20,
            width=100,
            height=20,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )

        response = self.client.patch(
            f"/api/admin/documents/{document.id}/fields/{field.id}/",
            {"x": 88, "y": 144, "is_required": False, "label": "Full Legal Name"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        field.refresh_from_db()
        self.assertEqual(field.x, 88)
        self.assertEqual(field.y, 144)
        self.assertFalse(field.is_required)
        self.assertEqual(field.label, "Full Legal Name")

    def test_delete_field_removes_it(self) -> None:
        document = Document.objects.create(
            title="Policy",
            original_pdf=SimpleUploadedFile("policy.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )
        field = DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Date",
            page=1,
            x=40,
            y=80,
            width=90,
            height=20,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )

        response = self.client.delete(
            f"/api/admin/documents/{document.id}/fields/{field.id}/"
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(DocumentField.objects.filter(id=field.id).exists())

    def test_send_document_creates_signing_requests_and_marks_document_sent(self) -> None:
        document = Document.objects.create(
            title="Offer Package",
            original_pdf=SimpleUploadedFile("offer.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )
        DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.SIGNATURE,
            label="Signature",
            page=1,
            x=72,
            y=120,
            width=180,
            height=36,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )

        response = self.client.post(
            f"/api/admin/documents/{document.id}/send/",
            {
                "signers": [
                    {"signer_email": "jane@example.com", "signer_name": "Jane Doe"},
                    {"signer_email": "john@example.com", "signer_name": "John Doe"},
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertEqual(payload["status"], "SENT")
        self.assertEqual(payload["signer_count"], 2)
        self.assertEqual(len(payload["signing_requests"]), 2)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.StatusEnum.SENT)
        self.assertEqual(document.signing_requests.count(), 2)

    def test_send_document_requires_at_least_one_field(self) -> None:
        document = Document.objects.create(
            title="Blank Contract",
            original_pdf=SimpleUploadedFile("blank.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )

        response = self.client.post(
            f"/api/admin/documents/{document.id}/send/",
            {"signers": [{"signer_email": "jane@example.com"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("fields", response.json())

    def test_send_document_rejects_already_sent_document(self) -> None:
        document = Document.objects.create(
            title="Existing Sent Doc",
            original_pdf=SimpleUploadedFile("sent.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            status=Document.StatusEnum.SENT,
        )
        DocumentField.objects.create(
            document=document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Name",
            page=1,
            x=72,
            y=120,
            width=180,
            height=24,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )
        SigningRequest.objects.create(
            document=document,
            signer_email="existing@example.com",
            signer_name="Existing User",
            expires_at=timezone.now(),
        )

        response = self.client.post(
            f"/api/admin/documents/{document.id}/send/",
            {"signers": [{"signer_email": "another@example.com"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("status", response.json())

    def test_patch_document_updates_title(self) -> None:
        document = Document.objects.create(
            title="Old Title",
            original_pdf=SimpleUploadedFile("doc.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
        )

        response = self.client.patch(
            f"/api/admin/documents/{document.id}/",
            {"title": "New Title"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        document.refresh_from_db()
        self.assertEqual(document.title, "New Title")

    def test_void_document_marks_pending_requests_invalid(self) -> None:
        document = Document.objects.create(
            title="To Void",
            original_pdf=SimpleUploadedFile("void.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            status=Document.StatusEnum.SENT,
        )
        request = SigningRequest.objects.create(
            document=document,
            signer_email="void@example.com",
            signer_name="Void User",
            expires_at=timezone.now() + timedelta(days=7),
        )

        response = self.client.post(
            f"/api/admin/documents/{document.id}/void/",
            {"voided_reason": "Sent in error"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        document.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(document.status, Document.StatusEnum.VOIDED)
        self.assertEqual(document.voided_reason, "Sent in error")
        self.assertIsNotNone(document.voided_at)
        self.assertLessEqual(request.expires_at, timezone.now())

    def test_download_signed_document_returns_file(self) -> None:
        document = Document.objects.create(
            title="Completed",
            original_pdf=SimpleUploadedFile("original.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            status=Document.StatusEnum.COMPLETED,
        )
        document.signed_pdf.save(
            "signed.pdf",
            ContentFile(build_flat_pdf()),
            save=True,
        )

        response = self.client.get(f"/api/admin/documents/{document.id}/download/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
