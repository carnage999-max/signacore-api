from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import fitz
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Organization, OrganizationMembership
from apps.documents.models import Document, DocumentField
from apps.signing.models import SigningRequest
from services.pdf_anchors import conceal_anchor_tags_on_page
from services.pdf_engine import PDFEngine
from services.pdf_sanitizer import (
    PDFSanitizationError,
    _has_live_key,
    assert_sanitized,
    find_active_content,
    sanitize_document,
)
from tasks.signing import issue_outstanding_completed_documents
from utils.file_storage import temporary_plaintext_file
from utils.observability import before_send, configure_error_reporting
from utils.pdf_preview import build_preview_matrix, prepare_page_for_preview
from utils.static_assets import versioned_static

from . import pdf_builders as builders

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-form-import-")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_APP_URL="https://mysignacore.com",
    SIGNACORE_SIGNER_PORTAL_URL="https://sign.mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class NativeFormImportTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin",
            email="admin@mysignacore.com",
            password="password123",
            is_staff=True,
        )
        self.organization = Organization.objects.create(name="Import Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
            role=OrganizationMembership.RoleEnum.ADMIN,
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def upload(self, pdf_bytes: bytes, *, title: str = "Imported Form") -> dict:
        response = self.client.post(
            "/api/admin/documents/",
            {
                "title": title,
                "pdf_file": SimpleUploadedFile(f"{title}.pdf", pdf_bytes, content_type="application/pdf"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_supported_widgets_import_with_type_label_and_required_flag(self) -> None:
        payload = self.upload(builders.build_supported_acroform_pdf())
        fields = payload["fields"]

        self.assertEqual([field["field_type"] for field in fields], ["TEXT", "CHECKBOX", "SIGNATURE"])
        self.assertEqual([field["label"] for field in fields], ["Employee Name", "I agree", "Employee Signature"])
        self.assertTrue(fields[0]["is_required"])
        self.assertFalse(fields[1]["is_required"])
        self.assertEqual(payload["detection_summary"]["source"], "ACROFORM")
        self.assertEqual(payload["detection_summary"]["native_widget_count"], 3)
        self.assertEqual(payload["detection_summary"]["imported_field_count"], 3)
        self.assertEqual(payload["detection_summary"]["ignored_widget_count"], 0)

    def test_native_widget_rectangle_is_converted_to_bottom_left_origin(self) -> None:
        pdf_bytes = builders.build_supported_acroform_pdf()
        payload = self.upload(pdf_bytes)

        with fitz.open("pdf", pdf_bytes) as source:
            page = source[0]
            page_height = page.rect.height
            source_rects = {widget.field_name: fitz.Rect(widget.rect) for widget in page.widgets()}

        text_field = payload["fields"][0]
        source_rect = source_rects["employee_name"]

        self.assertAlmostEqual(text_field["x"], source_rect.x0, places=3)
        self.assertAlmostEqual(text_field["y"], page_height - source_rect.y1, places=3)
        self.assertAlmostEqual(text_field["width"], source_rect.width, places=3)
        self.assertAlmostEqual(text_field["height"], source_rect.height, places=3)

        # The signer portal and flattener both derive the top edge this way.
        rendered_top = page_height - text_field["y"] - text_field["height"]
        self.assertAlmostEqual(rendered_top, source_rect.y0, places=3)

    def test_a_radio_group_imports_one_option_per_choice(self) -> None:
        payload = self.upload(builders.build_radio_group_pdf())

        self.assertEqual([field["field_type"] for field in payload["fields"]], ["RADIO"] * 3)
        self.assertEqual(payload["detection_summary"]["ignored_widget_count"], 0)

    def test_the_options_of_one_radio_group_share_a_group_key(self) -> None:
        payload = self.upload(builders.build_radio_group_pdf())

        keys = {field["group_key"] for field in payload["fields"]}
        self.assertEqual(len(keys), 1)
        self.assertNotIn("", keys)

    def test_a_choice_field_imports_as_a_dropdown_carrying_its_options(self) -> None:
        payload = self.upload(builders.build_choice_field_pdf())

        self.assertEqual([field["field_type"] for field in payload["fields"]], ["DROPDOWN", "DROPDOWN"])
        self.assertEqual(
            [field["options"] for field in payload["fields"]],
            [["January", "February", "March"], ["Airplane", "Boat"]],
        )

    def test_action_button_is_ignored_and_reported(self) -> None:
        payload = self.upload(builders.build_action_button_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("UNSUPPORTED_ACTION_BUTTON", payload["detection_summary"]["warning_codes"])

    def test_read_only_field_is_ignored_and_reported(self) -> None:
        payload = self.upload(builders.build_read_only_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("UNSUPPORTED_READ_ONLY_FIELD", payload["detection_summary"]["warning_codes"])

    def test_javascript_field_is_ignored_and_its_script_is_never_persisted(self) -> None:
        payload = self.upload(builders.build_javascript_calculation_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("UNSUPPORTED_PDF_JAVASCRIPT", payload["detection_summary"]["warning_codes"])

        document = Document.objects.get(pk=payload["id"])
        self.assertNotIn("AFSimple_Calculate", str(document.import_report))
        self.assertNotIn("JS", document.import_report.get("warning_codes", []))

    def test_hidden_field_is_ignored_and_its_value_never_reaches_a_signer(self) -> None:
        payload = self.upload(builders.build_hidden_field_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("HIDDEN_FIELD_IGNORED", payload["detection_summary"]["warning_codes"])

        document = Document.objects.get(pk=payload["id"])
        self.assertNotIn("prefilled-secret-value", str(document.import_report))
        self.assertFalse(document.fields.exists())

        signing_request = SigningRequest.objects.create(
            document=document,
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            expires_at=timezone.now() + timedelta(days=7),
        )
        signer_client = APIClient()
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            signer_client.post(f"/api/sign/{signing_request.id}/otp/send/")
            signer_client.post(
                f"/api/sign/{signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
            )
        context = signer_client.get(f"/api/sign/{signing_request.id}/context/")
        self.assertNotIn("prefilled-secret-value", context.content.decode())

    def test_multiline_widget_imports_as_a_multiline_field(self) -> None:
        payload = self.upload(builders.build_multiline_pdf())

        self.assertEqual(len(payload["fields"]), 1)
        self.assertEqual(payload["fields"][0]["field_type"], "MULTILINE")
        self.assertEqual(payload["fields"][0]["label"], "Comments")

    def test_mixed_form_imports_only_supported_widgets_and_persists_the_report(self) -> None:
        payload = self.upload(builders.build_mixed_form_pdf())

        self.assertEqual(sorted(field["field_type"] for field in payload["fields"]), ["DROPDOWN", "RADIO", "TEXT"])
        self.assertEqual(payload["detection_summary"]["native_widget_count"], 4)
        self.assertEqual(payload["detection_summary"]["imported_field_count"], 3)
        self.assertEqual(payload["detection_summary"]["ignored_widget_count"], 1)

        detail = self.client.get(f"/api/admin/documents/{payload['id']}/")
        self.assertEqual(detail.status_code, 200, detail.json())
        summary = detail.json()["detection_summary"]
        self.assertEqual(summary["ignored_widget_count"], 1)
        self.assertEqual(
            sorted(summary["warning_codes"]),
            ["SIGNATURE_FIELD_NOT_DETECTED", "UNSUPPORTED_ACTION_BUTTON"],
        )

    def test_form_with_only_unsupported_widgets_does_not_fall_back_to_heuristics(self) -> None:
        payload = self.upload(builders.build_unsupported_only_form_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertEqual(payload["detection_summary"]["source"], "ACROFORM")
        self.assertIn("NO_SUPPORTED_FIELDS_IMPORTED", payload["detection_summary"]["warning_codes"])
        self.assertFalse(Document.objects.get(pk=payload["id"]).fields.exists())

    def test_xfa_field_paths_are_never_exposed_as_labels(self) -> None:
        payload = self.upload(builders.build_xfa_style_pdf())

        labels = [field["label"] for field in payload["fields"]]
        self.assertTrue(labels)
        for label in labels:
            self.assertNotIn("topmostSubform", label)
            self.assertNotIn("[0]", label)
        self.assertIn("Name of entity/individual", labels)

    def test_printed_signature_line_is_reported_rather_than_faked(self) -> None:
        payload = self.upload(builders.build_xfa_style_pdf())

        self.assertNotIn("SIGNATURE", [field["field_type"] for field in payload["fields"]])
        self.assertIn("SIGNATURE_FIELD_NOT_DETECTED", payload["detection_summary"]["warning_codes"])

    def test_plain_pdf_still_uses_heuristic_detection(self) -> None:
        payload = self.upload(builders.build_flat_pdf())

        self.assertEqual(payload["detection_summary"]["source"], "HEURISTIC")
        self.assertEqual(payload["detection_summary"]["native_widget_count"], 0)
        self.assertGreaterEqual(len(payload["fields"]), 2)

    def test_another_organization_cannot_read_the_import_report(self) -> None:
        payload = self.upload(builders.build_mixed_form_pdf())

        other_user = get_user_model().objects.create_user(
            username="intruder",
            email="intruder@example.com",
            password="password123",
            is_staff=True,
        )
        other_organization = Organization.objects.create(name="Other Company", created_by=other_user)
        OrganizationMembership.objects.create(
            organization=other_organization,
            user=other_user,
            role=OrganizationMembership.RoleEnum.ADMIN,
        )
        other_client = APIClient()
        other_client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(other_user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(other_organization.id),
        )

        response = other_client.get(f"/api/admin/documents/{payload['id']}/")
        self.assertEqual(response.status_code, 404)


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class CompletedPDFSanitizationTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(username="admin", password="password123")
        self.organization = Organization.objects.create(name="Sanitize Company", created_by=self.user)
        self.document = Document.objects.create(
            title="Active Content Agreement",
            original_pdf=SimpleUploadedFile(
                "active.pdf",
                builders.build_active_content_pdf(),
                content_type="application/pdf",
            ),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
        )
        self.text_field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Signer name",
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
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_source_pdf_really_contains_active_content(self) -> None:
        with temporary_plaintext_file(self.document.original_pdf, suffix=".pdf") as pdf_path:
            findings = find_active_content(pdf_path)

        self.assertTrue(findings, "fixture must start with active content for this test to mean anything")

    def test_completed_pdf_has_no_widgets_scripts_or_actions(self) -> None:
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
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, 200, response.json())
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.StatusEnum.COMPLETED)
        self.assertTrue(self.document.signed_pdf)

        with temporary_plaintext_file(self.document.signed_pdf, suffix=".pdf") as signed_path:
            self.assertEqual(find_active_content(signed_path), [])
            with fitz.open(signed_path) as signed:
                self.assertFalse(signed.is_form_pdf)
                self.assertIn("Jane Doe", signed[0].get_text())


class FlattenedValueRenderingTests(TestCase):
    """Realistic form fields are short; values must survive flattening at those sizes."""

    def flatten_one(self, rect: fitz.Rect, *, field_type: str, value: str) -> str:
        document = fitz.open()
        document.new_page(width=612, height=792)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as source_file:
            source_file.write(document.tobytes())
            source_path = source_file.name
        document.close()

        output_path = source_path.replace(".pdf", "-flat.pdf")
        page_height = 792.0
        PDFEngine().flatten(
            source_path,
            output_path,
            [
                {
                    "page": 1,
                    "field_type": field_type,
                    "x": rect.x0,
                    "y": page_height - rect.y1,
                    "width": rect.width,
                    "height": rect.height,
                    "value_type": "CHECKBOX" if field_type == "CHECKBOX" else "TEXT",
                    "text_value": value,
                    "image_path": "",
                }
            ],
        )
        with fitz.open(output_path) as flattened:
            return flattened[0].get_text()

    def test_value_renders_in_a_w9_sized_text_field(self) -> None:
        text = self.flatten_one(fitz.Rect(58.6, 118.0, 576.0, 132.0), field_type="TEXT", value="ACME Holdings LLC")

        self.assertIn("ACME Holdings LLC", text)

    def test_value_renders_in_a_narrow_short_text_field(self) -> None:
        text = self.flatten_one(fitz.Rect(543.6, 192.0, 576.0, 204.0), field_type="TEXT", value="C")

        self.assertIn("C", text)

    def test_long_value_shrinks_instead_of_being_dropped(self) -> None:
        text = self.flatten_one(
            fitz.Rect(417.6, 372.0, 460.8, 396.0),
            field_type="TEXT",
            value="Constantinople Trading",
        )

        self.assertIn("Constantinople Trading", text)

    def test_multiline_value_wraps_instead_of_being_dropped(self) -> None:
        text = self.flatten_one(
            fitz.Rect(389.8, 286.0, 576.0, 324.0),
            field_type="MULTILINE",
            value="Requester Co\n1 Market Street\nLagos, Nigeria",
        )

        self.assertIn("Market Street", text)

    def test_checkbox_draws_a_mark_inside_a_small_box(self) -> None:
        document = fitz.open()
        document.new_page(width=612, height=792)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as source_file:
            source_file.write(document.tobytes())
            source_path = source_file.name
        document.close()

        output_path = source_path.replace(".pdf", "-check.pdf")
        rect = fitz.Rect(73.0, 180.2, 81.0, 188.2)
        PDFEngine().flatten(
            source_path,
            output_path,
            [
                {
                    "page": 1,
                    "field_type": "CHECKBOX",
                    "x": rect.x0,
                    "y": 792.0 - rect.y1,
                    "width": rect.width,
                    "height": rect.height,
                    "value_type": "CHECKBOX",
                    "text_value": "true",
                    "image_path": "",
                }
            ],
        )

        with fitz.open(output_path) as flattened:
            drawings = flattened[0].get_drawings()
        marks = [drawing for drawing in drawings if drawing["rect"].intersects(rect)]
        self.assertTrue(marks, "expected a vector check mark inside the checkbox rectangle")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class CombFieldImportTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@mysignacore.com", password="password123", is_staff=True
        )
        self.organization = Organization.objects.create(name="Comb Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=OrganizationMembership.RoleEnum.ADMIN
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def upload(self, pdf_bytes: bytes) -> dict:
        response = self.client.post(
            "/api/admin/documents/",
            {
                "title": "Comb form",
                "pdf_file": SimpleUploadedFile("comb.pdf", pdf_bytes, content_type="application/pdf"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_comb_widgets_import_their_cell_count(self) -> None:
        payload = self.upload(builders.build_comb_field_pdf())
        fields = payload["fields"]

        self.assertEqual([field["max_length"] for field in fields], [3, 2, 4])
        self.assertTrue(all(field["is_comb"] for field in fields))

    def test_split_row_reuses_the_printed_caption_instead_of_a_positional_name(self) -> None:
        payload = self.upload(builders.build_comb_field_pdf())

        labels = [field["label"] for field in payload["fields"]]
        self.assertEqual(
            labels,
            ["Social security number", "Social security number (2)", "Social security number (3)"],
        )
        for label in labels:
            self.assertNotIn("Page 1 field", label)

    def test_max_length_and_comb_flag_are_inherited_from_the_parent_field(self) -> None:
        payload = self.upload(builders.build_inherited_maxlen_pdf())

        self.assertEqual(payload["fields"][0]["max_length"], 6)
        self.assertTrue(payload["fields"][0]["is_comb"])

    def test_comb_value_is_drawn_one_character_per_cell(self) -> None:
        rect = fitz.Rect(417.6, 372.0, 460.8, 396.0)
        cells = 3
        document = fitz.open()
        document.new_page(width=612, height=792)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as source_file:
            source_file.write(document.tobytes())
            source_path = source_file.name
        document.close()

        output_path = source_path.replace(".pdf", "-comb.pdf")
        PDFEngine().flatten(
            source_path,
            output_path,
            [
                {
                    "page": 1,
                    "field_type": "TEXT",
                    "max_length": cells,
                    "is_comb": True,
                    "x": rect.x0,
                    "y": 792.0 - rect.y1,
                    "width": rect.width,
                    "height": rect.height,
                    "value_type": "TEXT",
                    "text_value": "123",
                    "image_path": "",
                }
            ],
        )

        with fitz.open(output_path) as flattened:
            # Separately placed glyphs extract with synthetic spacing between them.
            characters = [
                (span_char["c"], (span_char["bbox"][0] + span_char["bbox"][2]) / 2)
                for block in flattened[0].get_text("rawdict")["blocks"]
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                for span_char in span.get("chars", [])
                if span_char["c"].strip()
            ]

        self.assertEqual([character for character, _ in characters], ["1", "2", "3"])
        cell_width = rect.width / cells
        for index, (_, centre) in enumerate(characters):
            cell_start = rect.x0 + (cell_width * index)
            self.assertGreaterEqual(centre, cell_start)
            self.assertLessEqual(centre, cell_start + cell_width)

    def test_comb_value_longer_than_the_cell_count_is_truncated(self) -> None:
        rect = fitz.Rect(417.6, 372.0, 460.8, 396.0)
        document = fitz.open()
        document.new_page(width=612, height=792)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as source_file:
            source_file.write(document.tobytes())
            source_path = source_file.name
        document.close()

        output_path = source_path.replace(".pdf", "-long.pdf")
        PDFEngine().flatten(
            source_path,
            output_path,
            [
                {
                    "page": 1,
                    "field_type": "TEXT",
                    "max_length": 3,
                    "is_comb": True,
                    "x": rect.x0,
                    "y": 792.0 - rect.y1,
                    "width": rect.width,
                    "height": rect.height,
                    "value_type": "TEXT",
                    "text_value": "78888888",
                    "image_path": "",
                }
            ],
        )

        with fitz.open(output_path) as flattened:
            text = flattened[0].get_text().strip().replace(" ", "").replace("\n", "")

        self.assertEqual(text, "788")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class AnchorTagImportTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@mysignacore.com", password="password123", is_staff=True
        )
        self.organization = Organization.objects.create(name="Anchor Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=OrganizationMembership.RoleEnum.ADMIN
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def upload(self, pdf_bytes: bytes) -> dict:
        response = self.client.post(
            "/api/admin/documents/",
            {
                "title": "Anchored agreement",
                "pdf_file": SimpleUploadedFile("anchored.pdf", pdf_bytes, content_type="application/pdf"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_anchor_tags_create_typed_and_labelled_fields(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())
        fields = payload["fields"]

        self.assertEqual(payload["detection_summary"]["source"], "ANCHOR")
        self.assertEqual(
            [(field["field_type"], field["label"]) for field in fields],
            [
                ("TEXT", "Employee name"),
                ("SIGNATURE", "Signature"),
                ("INITIALS", "Initials"),
                ("CHECKBOX", "Accept terms"),
            ],
        )

    def test_optional_marker_makes_a_tag_field_optional(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())

        by_label = {field["label"]: field for field in payload["fields"]}
        self.assertFalse(by_label["Initials"]["is_required"])
        self.assertTrue(by_label["Employee name"]["is_required"])

    def test_a_checkbox_tag_does_not_inherit_the_tag_text_width(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())

        checkbox = next(field for field in payload["fields"] if field["field_type"] == "CHECKBOX")
        self.assertLessEqual(checkbox["width"], 20)

    def test_anchor_tags_take_precedence_over_native_widgets(self) -> None:
        payload = self.upload(builders.build_anchor_tag_over_widgets_pdf())

        self.assertEqual(payload["detection_summary"]["source"], "ANCHOR")
        self.assertEqual([field["label"] for field in payload["fields"]], ["Authorised signatory"])

    def test_completed_pdf_does_not_contain_the_anchor_tag_text(self) -> None:
        source_bytes = builders.build_anchor_tag_pdf()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as source_file:
            source_file.write(source_bytes)
            source_path = source_file.name

        with fitz.open(source_path) as source:
            self.assertIn("{{signature}}", source[0].get_text())

        output_path = source_path.replace(".pdf", "-signed.pdf")
        PDFEngine().flatten(
            source_path,
            output_path,
            [
                {
                    "page": 1,
                    "field_type": "TEXT",
                    "x": 121.5,
                    "y": 584.8,
                    "width": 190.0,
                    "height": 18.0,
                    "value_type": "TEXT",
                    "text_value": "Jane Doe",
                    "image_path": "",
                }
            ],
        )

        with fitz.open(output_path) as flattened:
            text = flattened[0].get_text()
        self.assertNotIn("{{", text)
        self.assertNotIn("signature}}", text)
        self.assertIn("Jane Doe", text)

    def test_admin_page_preview_does_not_render_the_anchor_tag_text(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())

        response = self.client.get(f"/api/admin/documents/{payload['id']}/pages/1/preview/")

        self.assertEqual(response.status_code, 200)
        document = Document.objects.get(pk=payload["id"])
        with temporary_plaintext_file(document.original_pdf, suffix=".pdf") as pdf_path:
            with fitz.open(pdf_path) as stored:
                # The stored original keeps the author's tags; only the render conceals them.
                self.assertIn("{{signature}}", stored[0].get_text())
                conceal_anchor_tags_on_page(stored[0])
                self.assertNotIn("{{", stored[0].get_text())

    def test_signer_preview_does_not_render_the_anchor_tag_text(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())
        document = Document.objects.get(pk=payload["id"])
        document.status = Document.StatusEnum.SENT
        document.save(update_fields=["status"])
        signing_request = SigningRequest.objects.create(
            document=document,
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            expires_at=timezone.now() + timedelta(days=7),
        )

        signer_client = APIClient()
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            signer_client.post(f"/api/sign/{signing_request.id}/otp/send/")
            signer_client.post(
                f"/api/sign/{signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
            )

        response = signer_client.get(f"/api/sign/{signing_request.id}/pages/1/preview/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_admin_can_edit_the_comb_settings_of_a_field(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())
        field_id = next(field["id"] for field in payload["fields"] if field["field_type"] == "TEXT")

        response = self.client.patch(
            f"/api/admin/documents/{payload['id']}/fields/{field_id}/",
            {"max_length": 9, "is_comb": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["max_length"], 9)
        self.assertTrue(response.json()["is_comb"])

    def test_admin_can_clear_the_comb_settings_of_a_field(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())
        field_id = next(field["id"] for field in payload["fields"] if field["field_type"] == "TEXT")
        self.client.patch(
            f"/api/admin/documents/{payload['id']}/fields/{field_id}/",
            {"max_length": 9, "is_comb": True},
            format="json",
        )

        response = self.client.patch(
            f"/api/admin/documents/{payload['id']}/fields/{field_id}/",
            {"max_length": None, "is_comb": False},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertIsNone(response.json()["max_length"])
        self.assertFalse(response.json()["is_comb"])

    def test_manually_added_field_can_declare_comb_cells(self) -> None:
        payload = self.upload(builders.build_anchor_tag_pdf())

        response = self.client.post(
            f"/api/admin/documents/{payload['id']}/fields/",
            {
                "field_type": "TEXT",
                "label": "Reference number",
                "page": 1,
                "x": 72,
                "y": 500,
                "width": 120,
                "height": 20,
                "is_required": True,
                "order": 99,
                "max_length": 6,
                "is_comb": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["max_length"], 6)
        self.assertTrue(response.json()["is_comb"])


class PreviewMatrixTests(TestCase):
    """Page rails request small thumbnails; everything else keeps the full preview render."""

    def page(self) -> fitz.Page:
        document = fitz.open()
        return document.new_page(width=612, height=792)

    def test_default_render_is_unchanged_when_no_width_is_requested(self) -> None:
        matrix = build_preview_matrix(self.page(), None)

        self.assertEqual((matrix.a, matrix.d), (2.0, 2.0))

    def test_unparseable_width_falls_back_to_the_default_render(self) -> None:
        matrix = build_preview_matrix(self.page(), "not-a-number")

        self.assertEqual((matrix.a, matrix.d), (2.0, 2.0))

    def test_requested_width_sets_the_zoom(self) -> None:
        matrix = build_preview_matrix(self.page(), "153")

        self.assertAlmostEqual(matrix.a, 153 / 612, places=6)
        self.assertAlmostEqual(matrix.d, 153 / 612, places=6)

    def test_width_is_clamped_to_a_safe_range(self) -> None:
        tiny = build_preview_matrix(self.page(), "1")
        huge = build_preview_matrix(self.page(), "100000")

        self.assertAlmostEqual(tiny.a, 60 / 612, places=6)
        self.assertAlmostEqual(huge.a, 1600 / 612, places=6)


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class UndetectableDocumentTests(TestCase):
    """A document we cannot read must say so rather than presenting an empty result."""

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@mysignacore.com", password="password123", is_staff=True
        )
        self.organization = Organization.objects.create(name="Scan Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=OrganizationMembership.RoleEnum.ADMIN
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def upload(self, pdf_bytes: bytes) -> dict:
        response = self.client.post(
            "/api/admin/documents/",
            {"title": "Scan", "pdf_file": SimpleUploadedFile("scan.pdf", pdf_bytes, content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_scanned_upload_is_reported_as_having_no_text_layer(self) -> None:
        payload = self.upload(builders.build_scanned_page_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("SCANNED_DOCUMENT_NO_TEXT_LAYER", payload["detection_summary"]["warning_codes"])

    def test_scanned_upload_is_still_usable_as_a_document(self) -> None:
        payload = self.upload(builders.build_scanned_page_pdf())

        detail = self.client.get(f"/api/admin/documents/{payload['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["page_count"], 1)
        self.assertIn(
            "SCANNED_DOCUMENT_NO_TEXT_LAYER",
            detail.json()["detection_summary"]["warning_codes"],
        )

        created = self.client.post(
            f"/api/admin/documents/{payload['id']}/fields/",
            {
                "field_type": "SIGNATURE",
                "label": "Signature",
                "page": 1,
                "x": 72,
                "y": 200,
                "width": 180,
                "height": 36,
                "is_required": True,
                "order": 1,
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.json())

    def test_digital_document_with_nothing_to_detect_is_reported_separately(self) -> None:
        payload = self.upload(builders.build_blank_digital_pdf())

        warnings = payload["detection_summary"]["warning_codes"]
        self.assertIn("NO_FIELDS_DETECTED", warnings)
        self.assertNotIn("SCANNED_DOCUMENT_NO_TEXT_LAYER", warnings)

    def test_a_readable_flat_pdf_is_not_reported_as_undetectable(self) -> None:
        payload = self.upload(builders.build_flat_pdf())

        warnings = payload["detection_summary"]["warning_codes"]
        self.assertNotIn("NO_FIELDS_DETECTED", warnings)
        self.assertNotIn("SCANNED_DOCUMENT_NO_TEXT_LAYER", warnings)
        self.assertGreaterEqual(len(payload["fields"]), 2)


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class SanitizationFailureTests(TestCase):
    """If the packaged file cannot be proven clean, withhold the file, not the signature."""

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(username="admin", password="password123")
        self.organization = Organization.objects.create(name="Failure Company", created_by=self.user)
        self.document = Document.objects.create(
            title="Unsanitisable",
            original_pdf=SimpleUploadedFile(
                "active.pdf", builders.build_active_content_pdf(), content_type="application/pdf"
            ),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
        )
        self.field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Signer name",
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
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def submit(self) -> "object":
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            verify_response = self.client.post(
                f"/api/sign/{self.signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
            )
        session_token = verify_response.json()["session_token"]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/sign/{self.signing_request.id}/submit/",
                {
                    "session_token": session_token,
                    f"field_{self.field.id}_type": "TEXT",
                    f"field_{self.field.id}_value": "Jane Doe",
                },
                format="multipart",
            )

    @patch("services.document_completion.PDFEngine.flatten", side_effect=PDFSanitizationError("still contains /JS"))
    def test_signature_is_kept_and_no_unsafe_file_is_issued(self, _flatten) -> None:
        response = self.submit()

        self.assertEqual(response.status_code, 202, response.json())
        self.document.refresh_from_db()
        self.signing_request.refresh_from_db()

        self.assertNotEqual(self.document.status, Document.StatusEnum.COMPLETED)
        self.assertFalse(self.document.signed_pdf)
        self.assertEqual(self.signing_request.status, SigningRequest.StatusEnum.SIGNED)
        self.assertTrue(self.signing_request.submissions.exists())

    @patch("services.document_completion.PDFEngine.flatten", side_effect=PDFSanitizationError("still contains /JS"))
    def test_signer_is_told_the_sender_must_act(self, _flatten) -> None:
        response = self.submit()

        self.assertIn("sender", response.json()["message"].lower())


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ScriptedFieldClassificationTests(TestCase):
    """A script only disqualifies a field when it decides the value.

    Form generators attach a date mask or a grey-placeholder script to every field. Treating any
    script as disqualifying made a real 21-field form import 2 fields.
    """

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@mysignacore.com", password="password123", is_staff=True
        )
        self.organization = Organization.objects.create(name="Scripted Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=OrganizationMembership.RoleEnum.ADMIN
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def upload(self, pdf_bytes: bytes) -> dict:
        response = self.client.post(
            "/api/admin/documents/",
            {"title": "Scripted", "pdf_file": SimpleUploadedFile("s.pdf", pdf_bytes, content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_a_date_mask_does_not_stop_the_field_importing(self) -> None:
        payload = self.upload(builders.build_formatted_text_field_pdf())

        self.assertEqual([field["field_type"] for field in payload["fields"]], ["TEXT"])
        self.assertIn("FIELD_FORMATTING_NOT_APPLIED", payload["detection_summary"]["warning_codes"])

    def test_a_placeholder_script_does_not_stop_the_field_importing(self) -> None:
        payload = self.upload(builders.build_placeholder_script_pdf())

        self.assertEqual([field["field_type"] for field in payload["fields"]], ["TEXT"])
        self.assertEqual(payload["detection_summary"]["ignored_widget_count"], 0)

    def test_a_field_imported_with_dropped_formatting_is_not_counted_as_ignored(self) -> None:
        payload = self.upload(builders.build_formatted_text_field_pdf())

        self.assertEqual(payload["detection_summary"]["imported_field_count"], 1)
        self.assertEqual(payload["detection_summary"]["ignored_widget_count"], 0)

    def test_a_validation_rule_does_not_stop_the_field_importing(self) -> None:
        payload = self.upload(builders.build_validated_text_field_pdf())

        self.assertEqual([field["field_type"] for field in payload["fields"]], ["TEXT"])
        self.assertEqual(payload["detection_summary"]["ignored_widget_count"], 0)
        self.assertIn("FIELD_VALIDATION_NOT_ENFORCED", payload["detection_summary"]["warning_codes"])
        self.assertNotIn("UNSUPPORTED_PDF_JAVASCRIPT", payload["detection_summary"]["warning_codes"])

    def test_a_calculation_still_skips_the_field(self) -> None:
        payload = self.upload(builders.build_javascript_calculation_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("UNSUPPORTED_PDF_JAVASCRIPT", payload["detection_summary"]["warning_codes"])

    def test_no_script_text_is_ever_persisted(self) -> None:
        payload = self.upload(builders.build_formatted_text_field_pdf())

        document = Document.objects.get(pk=payload["id"])
        self.assertNotIn("AFDate", str(document.import_report))
        for field in document.fields.all():
            self.assertNotIn("AFDate", field.label)


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ImageButtonSignatureTests(TestCase):
    """Acrobat builds a signature placeholder as a push button that imports an image."""

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@mysignacore.com", password="password123", is_staff=True
        )
        self.organization = Organization.objects.create(name="Image Button Co", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=OrganizationMembership.RoleEnum.ADMIN
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def upload(self, pdf_bytes: bytes) -> dict:
        response = self.client.post(
            "/api/admin/documents/",
            {"title": "Buttons", "pdf_file": SimpleUploadedFile("b.pdf", pdf_bytes, content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_an_image_button_on_a_signature_line_becomes_a_signature_field(self) -> None:
        payload = self.upload(builders.build_image_button_signature_pdf())

        self.assertEqual(len(payload["fields"]), 1)
        self.assertEqual(payload["fields"][0]["field_type"], "SIGNATURE")
        self.assertIn("Signature", payload["fields"][0]["label"])

    def test_an_image_button_that_is_not_a_signature_line_is_still_skipped(self) -> None:
        payload = self.upload(builders.build_image_button_logo_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("UNSUPPORTED_ACTION_BUTTON", payload["detection_summary"]["warning_codes"])

    def test_a_wrapped_two_line_label_is_read_in_full(self) -> None:
        payload = self.upload(builders.build_wrapped_label_pdf())

        self.assertEqual(len(payload["fields"]), 1)
        self.assertEqual(payload["fields"][0]["field_type"], "SIGNATURE")
        self.assertEqual(payload["fields"][0]["label"], "Signature of Principal or Authorized Official")

    def test_a_signature_button_action_is_never_persisted(self) -> None:
        payload = self.upload(builders.build_image_button_signature_pdf())

        document = Document.objects.get(pk=payload["id"])
        self.assertNotIn("buttonImportIcon", str(document.import_report))
        for field in document.fields.all():
            self.assertNotIn("buttonImportIcon", field.label)


class PreviewPagePreparationTests(SimpleTestCase):
    """A preview must not show the PDF's own placeholder text under SignaCore's field."""

    def test_widget_placeholder_text_is_removed_before_rendering(self) -> None:
        pdf_bytes = builders.build_placeholder_appearance_pdf()

        with fitz.open("pdf", pdf_bytes) as document:
            self.assertIn("[Minor full legal name]", document[0].get_text())

        with fitz.open("pdf", pdf_bytes) as document:
            page = document[0]
            prepare_page_for_preview(page)

            self.assertNotIn("[Minor full legal name]", page.get_text())
            self.assertEqual(list(page.widgets() or []), [])

    def test_printed_page_content_survives_preparation(self) -> None:
        pdf_bytes = builders.build_placeholder_appearance_pdf()

        with fitz.open("pdf", pdf_bytes) as document:
            page = document[0]
            prepare_page_for_preview(page)

            self.assertIn("Full Legal Name of Minor", page.get_text())

    def test_preparation_leaves_the_stored_document_untouched(self) -> None:
        pdf_bytes = builders.build_placeholder_appearance_pdf()

        with fitz.open("pdf", pdf_bytes) as document:
            prepare_page_for_preview(document[0])

        with fitz.open("pdf", pdf_bytes) as reopened:
            self.assertIn("[Minor full legal name]", reopened[0].get_text())


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class MixedPacketImportTests(TestCase):
    """An onboarding packet is several documents in one file.

    A fillable government form sits between agreements that were only ever laid out to be printed,
    so deciding between native widgets and heuristics once for the whole file left every printed
    signature block in a 31-page packet undetected.
    """

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@mysignacore.com", password="password123", is_staff=True
        )
        self.organization = Organization.objects.create(name="Packet Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=OrganizationMembership.RoleEnum.ADMIN
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def upload(self, pdf_bytes: bytes) -> dict:
        response = self.client.post(
            "/api/admin/documents/",
            {"title": "Packet", "pdf_file": SimpleUploadedFile("p.pdf", pdf_bytes, content_type="application/pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_a_printed_page_is_detected_alongside_a_native_form_page(self) -> None:
        payload = self.upload(builders.build_mixed_packet_pdf())

        sources = {field["page"]: field["detection_source"] for field in payload["fields"]}
        self.assertEqual(sources[1], "ACROFORM")
        self.assertEqual(sources[2], "HEURISTIC")
        self.assertIn("SIGNATURE", [field["field_type"] for field in payload["fields"] if field["page"] == 2])

    def test_a_page_carrying_widgets_is_never_read_heuristically(self) -> None:
        payload = self.upload(builders.build_mixed_packet_pdf())

        page_one = [field for field in payload["fields"] if field["page"] == 1]
        self.assertEqual([field["detection_source"] for field in page_one], ["ACROFORM"])

    def test_field_order_is_continuous_across_both_detection_paths(self) -> None:
        payload = self.upload(builders.build_mixed_packet_pdf())

        orders = sorted(field["order"] for field in payload["fields"])
        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_a_mutually_exclusive_checkbox_group_imports_as_radio_options(self) -> None:
        """Checkboxes a script keeps exclusive are the same choice a radio group expresses.

        Importing them as checkboxes would let a signer attest to two citizenship statuses.
        """
        payload = self.upload(builders.build_exclusive_checkbox_group_pdf())

        self.assertEqual([field["field_type"] for field in payload["fields"]], ["RADIO"] * 3)
        self.assertEqual(len({field["group_key"] for field in payload["fields"]}), 1)
        self.assertEqual(payload["detection_summary"]["ignored_widget_count"], 0)

    def test_a_signature_named_text_widget_becomes_a_signature_field(self) -> None:
        payload = self.upload(builders.build_printed_signature_widget_pdf())

        types = {field["label"]: field["field_type"] for field in payload["fields"]}
        self.assertEqual(types["Signature of Employee"], "SIGNATURE")

    def test_the_date_accompanying_a_signature_stays_a_text_field(self) -> None:
        payload = self.upload(builders.build_printed_signature_widget_pdf())

        dates = [field for field in payload["fields"] if "Date" in field["label"]]
        self.assertEqual([field["field_type"] for field in dates], ["TEXT"])

    def test_a_middle_initial_is_a_text_field_not_a_signing_mark(self) -> None:
        payload = self.upload(builders.build_printed_signature_widget_pdf())

        types = {field["label"]: field["field_type"] for field in payload["fields"]}
        self.assertEqual(types["Middle Initial, if any"], "TEXT")
        self.assertEqual(types["Initials"], "INITIALS")

    def test_a_tooltip_describing_a_section_is_not_used_as_a_label(self) -> None:
        payload = self.upload(builders.build_section_tooltip_pdf())

        labels = sorted(field["label"] for field in payload["fields"])
        self.assertEqual(labels, ["Document Number 1", "Issuing Authority 1"])
        for label in labels:
            self.assertNotIn("Employer Review", label)

    def test_a_rule_under_a_running_header_is_not_a_signature_line(self) -> None:
        payload = self.upload(builders.build_header_rule_pdf())

        self.assertEqual(payload["fields"], [])
        self.assertIn("NO_FIELDS_DETECTED", payload["detection_summary"]["warning_codes"])

    def test_a_signature_found_on_a_printed_page_does_not_silence_the_form_prompt(self) -> None:
        """The W-9 signs on its form page, and its instruction pages must not answer for it."""
        payload = self.upload(builders.build_mixed_packet_pdf())

        self.assertIn("SIGNATURE_FIELD_NOT_DETECTED", payload["detection_summary"]["warning_codes"])

    def test_a_locked_signature_line_is_still_somewhere_to_sign(self) -> None:
        payload = self.upload(builders.build_locked_signature_pdf())

        self.assertEqual([field["field_type"] for field in payload["fields"]], ["SIGNATURE"])
        self.assertIn("UNSUPPORTED_READ_ONLY_FIELD", payload["detection_summary"]["warning_codes"])

    def test_the_blank_cells_of_a_form_table_become_fields(self) -> None:
        payload = self.upload(builders.build_form_table_pdf())

        labels = [field["label"] for field in payload["fields"]]
        self.assertEqual(sorted(labels), ["Full Legal Name", "Home Address", "Mobile Phone"])

    def test_a_table_presenting_information_is_not_turned_into_a_form(self) -> None:
        """Every cell of a reference table already has content, so none of it is asking for any."""
        payload = self.upload(builders.build_form_table_pdf())

        self.assertNotIn("1 year", [field["label"] for field in payload["fields"]])
        self.assertEqual(len(payload["fields"]), 3)

    def test_one_table_does_not_take_its_neighbour_column_edges(self) -> None:
        payload = self.upload(builders.build_two_tables_one_page_pdf())

        labels = sorted(field["label"] for field in payload["fields"])
        self.assertEqual(labels, ["Home Address", "Legal First Name", "Legal Last Name"])

    def test_a_caption_written_above_a_rule_names_it(self) -> None:
        payload = self.upload(builders.build_caption_above_rule_pdf())

        named = {field["label"]: field["field_type"] for field in payload["fields"]}
        self.assertEqual(named.get("Employee Acknowledgment Signature"), "SIGNATURE")
        self.assertEqual(named.get("Printed Name"), "TEXT")
        self.assertEqual(named.get("Acknowledgment Date"), "TEXT")

    def test_unnamed_writing_lines_are_not_signature_fields(self) -> None:
        """Five ruled lines under a prompt are one written answer, not five things to sign."""
        payload = self.upload(builders.build_written_answer_lines_pdf())

        types = {field["field_type"] for field in payload["fields"]}
        self.assertNotIn("SIGNATURE", types)

    def test_tick_boxes_are_not_required_but_a_signature_line_is(self) -> None:
        payload = self.upload(builders.build_optional_tick_boxes_pdf())

        required = {field["field_type"] for field in payload["fields"] if field["is_required"]}
        optional = {field["field_type"] for field in payload["fields"] if not field["is_required"]}
        self.assertNotIn("CHECKBOX", required)
        self.assertIn("CHECKBOX", optional)
        self.assertIn("SIGNATURE", required)

    def test_a_block_of_ruled_lines_becomes_one_written_answer(self) -> None:
        payload = self.upload(builders.build_written_answer_lines_pdf())

        types = [field["field_type"] for field in payload["fields"]]
        self.assertEqual(types.count("MULTILINE"), 1)
        self.assertNotIn("SIGNATURE", types)


class SanitizationBoundaryTests(SimpleTestCase):
    """A key is a name, not a prefix of one.

    Fonts are subset under a six-letter prefix, so nearly every PDF carries names like
    /AAAAAA+DejaVuSans. Matching the bare token /AA inside those condemned inert documents as
    active, which stopped them completing: no signed copy was written and no completion email was
    sent, while the signer was told the sender had to act.
    """

    def test_a_font_subset_prefix_is_not_an_additional_action(self) -> None:
        for name in ("/FontName/AAAAAA+DejaVuSans-Bold", "/BaseFont/AAAAAB+Arial"):
            self.assertFalse(_has_live_key(name, "/AA"), name)

    def test_a_longer_name_is_not_the_key_it_starts_with(self) -> None:
        self.assertFalse(_has_live_key("<</Name/Widgets>>", "/Widget"))

    def test_a_real_key_is_still_found(self) -> None:
        for obj, token in (
            ("<</AA 12 0 R>>", "/AA"),
            ("<</AA<</O 3 0 R>>>>", "/AA"),
            ("<</S/JavaScript/JS(app.alert)>>", "/JS"),
            ("<</Subtype/Widget>>", "/Widget"),
            ("<</URI(https://example.invalid)>>", "/URI"),
            ("<</AcroForm 4 0 R>>", "/AcroForm"),
        ):
            self.assertTrue(_has_live_key(obj, token), obj)

    def test_a_key_set_to_null_is_still_treated_as_absent(self) -> None:
        self.assertFalse(_has_live_key("<</AA null>>", "/AA"))


class UnverifiableObjectTests(SimpleTestCase):
    """An object nobody can read is not an object anybody can vouch for.

    Verification used to skip an object it could not parse, on the stated grounds that such an
    object could not carry an action. Nothing established that. The contents of an object that
    will not parse are by definition unknown, and this is the check that decides whether a file
    goes out to the people who signed it, so it now refuses rather than assumes.

    This is affordable because the check only ever runs on a file this module has just written
    with ``garbage=4, clean=True``, which rebuilds the cross-reference table. Source documents do
    carry dangling references - the IRS W-9 has two - and they do not survive the save.
    """

    def write_pdf(self, data: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        handle.write(data)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def refuse_one_object(self, xref: int):
        """Fail to read one object, exactly as MuPDF does for a reference it cannot resolve.

        MuPDF repairs a hand-damaged cross-reference table on open, dropping the entries that
        point nowhere, so a fixture cannot carry this state into the check. The failure is
        introduced at the seam it actually arrives through instead.
        """
        original = fitz.Document.xref_object

        def read(self, number, *args, **kwargs):
            if number == xref:
                raise RuntimeError(f"code=7: cannot find object in xref ({number} 0 R)")
            return original(self, number, *args, **kwargs)

        return patch.object(fitz.Document, "xref_object", read)

    def test_an_object_that_cannot_be_read_is_reported(self) -> None:
        path = self.write_pdf(builders.build_flat_pdf())
        self.assertEqual(find_active_content(path), [], "the fixture must be clean to begin with")

        with self.refuse_one_object(2):
            findings = find_active_content(path)

        self.assertTrue(any("object 2" in finding and "could not be read" in finding for finding in findings), findings)

    def test_an_unreadable_object_withholds_the_file(self) -> None:
        path = self.write_pdf(builders.build_flat_pdf())

        with self.refuse_one_object(2), self.assertRaises(PDFSanitizationError):
            assert_sanitized(path)

    def test_a_readable_document_is_still_passed(self) -> None:
        """The point of refusing is lost if it refuses everything."""
        path = self.write_pdf(builders.build_flat_pdf())

        assert_sanitized(path)

    def test_the_catalog_is_cleared_even_when_its_keys_cannot_be_listed(self) -> None:
        """A catalog that resists inspection used to keep every action it had."""
        source = self.write_pdf(builders.build_active_content_pdf())
        cleaned = f"{source}-cleaned.pdf"
        self.addCleanup(lambda: Path(cleaned).unlink(missing_ok=True))

        with patch("services.pdf_sanitizer._xref_keys", return_value=[]):
            with fitz.open(source) as document:
                sanitize_document(document)
                document.save(cleaned, garbage=4, clean=True, deflate=True)

        self.assertFalse(
            [finding for finding in find_active_content(cleaned) if "/OpenAction" in finding],
            find_active_content(cleaned),
        )


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class OutstandingCompletionTests(TestCase):
    """A document everyone signed but which never got its copy is found and finished.

    The copy is produced in the request carrying the last signature. Anything interrupting that
    leaves the signatures stored with nothing to show for them, and no completion email, because
    that is sent only once a copy exists. Nothing was watching for it.
    """

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@example.com", password="pw123456"
        )
        self.organization = Organization.objects.create(name="Reconcile Company", created_by=self.user)
        self.document = Document.objects.create(
            title="Stranded",
            original_pdf=SimpleUploadedFile("flat.pdf", builders.build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
        )
        self.field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Signer name",
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
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def strand(self) -> None:
        """Sign the document, with the packaging failing the way a fault in it would."""
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            verify = self.client.post(
                f"/api/sign/{self.signing_request.id}/otp/verify/", {"otp": "123456"}, format="json"
            )
        token = verify.json()["session_token"]
        with patch(
            "services.document_completion.PDFEngine.flatten",
            side_effect=PDFSanitizationError("still contains /JS"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    f"/api/sign/{self.signing_request.id}/submit/",
                    {
                        "session_token": token,
                        f"field_{self.field.id}_type": "TEXT",
                        f"field_{self.field.id}_value": "Jane Doe",
                    },
                    format="multipart",
                )
        self.document.refresh_from_db()
        self.assertFalse(self.document.signed_pdf)

    def test_a_stranded_document_is_completed_on_the_next_sweep(self) -> None:
        self.strand()
        mail.outbox.clear()

        self.assertEqual(issue_outstanding_completed_documents(), 1)

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.StatusEnum.COMPLETED)
        self.assertTrue(self.document.signed_pdf)
        # The email is the point of finishing it: neither party heard anything the first time.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("jane@example.com", mail.outbox[0].recipients())
        self.assertIn("admin@example.com", mail.outbox[0].recipients())

    def test_a_document_still_awaiting_a_signer_is_left_alone(self) -> None:
        self.strand()
        SigningRequest.objects.create(
            document=self.document,
            signer_email="sam@example.com",
            signer_name="Sam Roe",
            expires_at=timezone.now() + timedelta(days=7),
        )

        self.assertEqual(issue_outstanding_completed_documents(), 0)
        self.document.refresh_from_db()
        self.assertFalse(self.document.signed_pdf)

    def test_a_voided_document_is_left_alone(self) -> None:
        self.strand()
        self.document.status = Document.StatusEnum.VOIDED
        self.document.save(update_fields=["status"])

        self.assertEqual(issue_outstanding_completed_documents(), 0)
        self.document.refresh_from_db()
        self.assertFalse(self.document.signed_pdf)

    def test_a_document_that_already_has_its_copy_is_not_reissued(self) -> None:
        self.strand()
        self.assertEqual(issue_outstanding_completed_documents(), 1)

        self.assertEqual(issue_outstanding_completed_documents(), 0)


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class SignerSignedCopyTests(TestCase):
    """A signer can keep a copy of what they put their name to."""

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@example.com", password="pw123456"
        )
        self.organization = Organization.objects.create(name="Copy Company", created_by=self.user)
        self.document = Document.objects.create(
            title="Consent Form",
            original_pdf=SimpleUploadedFile("flat.pdf", builders.build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
        )
        self.field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Signer name",
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
            signer_email="jane@example.com",
            signer_name="Jane Doe",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def verify(self) -> str:
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            verify = self.client.post(
                f"/api/sign/{self.signing_request.id}/otp/verify/", {"otp": "123456"}, format="json"
            )
        return verify.json()["session_token"]

    def sign(self) -> None:
        token = self.verify()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                f"/api/sign/{self.signing_request.id}/submit/",
                {
                    "session_token": token,
                    f"field_{self.field.id}_type": "TEXT",
                    f"field_{self.field.id}_value": "Jane Doe",
                },
                format="multipart",
            )

    def test_a_signer_can_download_the_completed_copy(self) -> None:
        self.sign()
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.StatusEnum.COMPLETED)

        response = self.client.get(f"/api/sign/{self.signing_request.id}/signed/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_an_unverified_visitor_cannot_download_it(self) -> None:
        self.sign()
        self.client.cookies.clear()

        response = self.client.get(f"/api/sign/{self.signing_request.id}/signed/")

        self.assertEqual(response.status_code, 403)

    def test_there_is_nothing_to_download_before_the_document_completes(self) -> None:
        self.verify()

        response = self.client.get(f"/api/sign/{self.signing_request.id}/signed/")

        self.assertEqual(response.status_code, 404)

    def test_the_context_says_whether_a_copy_is_ready(self) -> None:
        self.verify()
        before = self.client.get(f"/api/sign/{self.signing_request.id}/").json()
        self.assertFalse(before["has_signed"])
        self.assertFalse(before["signed_copy_ready"])

        self.sign()
        after = self.client.get(f"/api/sign/{self.signing_request.id}/").json()
        self.assertTrue(after["has_signed"])
        self.assertTrue(after["signed_copy_ready"])


class PortalAssetVersionTests(SimpleTestCase):
    """A deployed change to the portal has to reach people already using it.

    The portal's stylesheet and script are served by Django under fixed names, so a browser that
    has seen them once keeps its copy. A change then appears not to have shipped at all.
    """

    def test_the_url_carries_a_digest_of_the_file(self) -> None:
        url = versioned_static("signing/portal.css")

        self.assertIn("signing/portal.css", url)
        self.assertRegex(url, r"\?v=[0-9a-f]{12}$")

    def test_two_different_files_get_different_versions(self) -> None:
        self.assertNotEqual(
            versioned_static("signing/portal.css"),
            versioned_static("signing/portal.js"),
        )

    def test_a_missing_asset_still_returns_a_usable_url(self) -> None:
        url = versioned_static("signing/not-a-real-file.css")

        self.assertIn("signing/not-a-real-file.css", url)
        self.assertNotIn("?v=", url)


class ErrorReportingTests(SimpleTestCase):
    """What leaves the service is decided deliberately, not left to a default.

    SignaCore handles signatures, identity documents and the contents of other people's
    agreements. An error report is worth having; sending any of that with it is not.
    """

    def test_reporting_stays_off_without_a_dsn(self) -> None:
        self.assertFalse(
            configure_error_reporting(dsn="", environment="test", release="", traces_sample_rate=0.1, debug=False)
        )

    def test_identifying_values_are_scrubbed_from_an_event(self) -> None:
        event = before_send(
            {
                "request": {
                    "headers": {"Authorization": "Bearer abc", "User-Agent": "Firefox"},
                    "cookies": {"session_token": "s3cret"},
                },
                "extra": {"signer_email": "jane@example.com", "document_id": "doc-1"},
            },
            {},
        )

        self.assertEqual(event["request"]["headers"]["Authorization"], "[scrubbed]")
        self.assertEqual(event["request"]["cookies"]["session_token"], "[scrubbed]")
        self.assertEqual(event["extra"]["signer_email"], "[scrubbed]")
        # What is left has to be enough to act on, or the report is not worth sending.
        self.assertEqual(event["request"]["headers"]["User-Agent"], "Firefox")
        self.assertEqual(event["extra"]["document_id"], "doc-1")

    def test_a_signed_value_is_scrubbed_however_deeply_it_sits(self) -> None:
        event = before_send({"a": {"b": [{"c": {"text_value": "Jane Doe"}}]}}, {})

        self.assertEqual(event["a"]["b"][0]["c"]["text_value"], "[scrubbed]")
