"""Filling in your own part of an agreement before you send it.

Most of a contract is the sender's to complete - the property, the dates, the amounts. Asking
the signer for it is both work they cannot do and an invitation to get it wrong. These tests
cover what the sender may fill, what they may never fill, and that what they filled survives
into the document everybody ends up holding.
"""

from __future__ import annotations

import tempfile

import fitz
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import AccountProfile, Organization, OrganizationMembership
from apps.documents.models import Document, DocumentField
from apps.signing.models import FieldSubmission, SigningRequest
from utils.file_storage import temporary_plaintext_file

from .test_admin_documents import build_flat_pdf

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-prefill-media-")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
    SIGNACORE_TEST_OTP_CODE="123456",
)
class SenderPrefillTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="prefill-admin",
            email="owner@example.com",
            password="password123",
            is_staff=True,
        )
        AccountProfile.objects.create(
            user=self.user,
            account_type=AccountProfile.AccountTypeEnum.COMPANY,
            email="owner@example.com",
            display_name="Priya Shah",
        )
        self.organization = Organization.objects.create(name="Prefill Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        self.document = Document.objects.create(
            title="Lease Agreement",
            original_pdf=SimpleUploadedFile("lease.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.DRAFT,
        )
        self.landlord_field = self.make_field(DocumentField.FieldTypeEnum.TEXT, "Monthly rent", order=1, y=620)
        self.tenant_field = self.make_field(DocumentField.FieldTypeEnum.TEXT, "Tenant full name", order=2, y=560)
        self.signature_field = self.make_field(
            DocumentField.FieldTypeEnum.SIGNATURE, "Tenant signature", order=3, y=500, height=40
        )
        self.as_workspace()

    def make_field(self, field_type, label, *, order, y, height=24) -> DocumentField:
        return DocumentField.objects.create(
            document=self.document,
            field_type=field_type,
            label=label,
            page=1,
            x=72,
            y=y,
            width=180,
            height=height,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.HEURISTIC,
            order=order,
        )

    def as_workspace(self) -> None:
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def prefill(self, field: DocumentField, value: str):
        return self.client.patch(
            f"/api/admin/documents/{self.document.id}/fields/{field.id}/",
            {"prefilled_value": value},
            format="json",
        )

    # ------------------------------------------------------------------ what a sender may fill

    def test_the_sender_can_answer_their_own_part(self) -> None:
        response = self.prefill(self.landlord_field, "1,450.00")

        self.assertEqual(response.status_code, 200, response.content)
        self.landlord_field.refresh_from_db()
        self.assertEqual(self.landlord_field.prefilled_value, "1,450.00")
        self.assertTrue(self.landlord_field.is_prefilled)

    def test_who_filled_it_and_when_is_recorded(self) -> None:
        """A value on an agreement that nobody is named against is what a dispute turns on."""
        before = timezone.now()
        self.prefill(self.landlord_field, "1,450.00")

        self.landlord_field.refresh_from_db()
        self.assertEqual(self.landlord_field.prefilled_by, self.user)
        self.assertGreaterEqual(self.landlord_field.prefilled_at, before)

    def test_clearing_a_value_clears_who_filled_it(self) -> None:
        self.prefill(self.landlord_field, "1,450.00")
        self.prefill(self.landlord_field, "")

        self.landlord_field.refresh_from_db()
        self.assertFalse(self.landlord_field.is_prefilled)
        self.assertIsNone(self.landlord_field.prefilled_by)
        self.assertIsNone(self.landlord_field.prefilled_at)

    # ------------------------------------------------------------- what a sender may never fill

    def test_a_sender_cannot_make_the_signers_signature(self) -> None:
        """The line the whole product rests on. One party producing another party's mark is
        forgery, whatever the interface called it."""
        response = self.prefill(self.signature_field, "Avery Morgan")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("prefilled_value", response.json())
        self.signature_field.refresh_from_db()
        self.assertFalse(self.signature_field.is_prefilled)

    def test_a_sender_cannot_make_the_signers_initials(self) -> None:
        initials = self.make_field(DocumentField.FieldTypeEnum.INITIALS, "Initial here", order=4, y=460)

        response = self.prefill(initials, "AM")

        self.assertEqual(response.status_code, 400, response.content)
        initials.refresh_from_db()
        self.assertFalse(initials.is_prefilled)

    def test_a_field_cannot_be_turned_into_a_signature_and_filled_at_once(self) -> None:
        """Changing the type in the same request must not slip past the check."""
        response = self.client.patch(
            f"/api/admin/documents/{self.document.id}/fields/{self.landlord_field.id}/",
            {"field_type": DocumentField.FieldTypeEnum.SIGNATURE, "prefilled_value": "Avery Morgan"},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.landlord_field.refresh_from_db()
        self.assertEqual(self.landlord_field.field_type, DocumentField.FieldTypeEnum.TEXT)
        self.assertFalse(self.landlord_field.is_prefilled)

    # --------------------------------------------------------------- what the signer then sees

    def send_and_verify(self) -> tuple[SigningRequest, str]:
        self.client.post(
            f"/api/admin/documents/{self.document.id}/send/",
            {"signers": [{"signer_email": "tenant@example.com", "signer_name": "Avery Morgan"}]},
            format="json",
        )
        signing_request = self.document.signing_requests.get()
        signer = APIClient()
        signer.post(f"/api/sign/{signing_request.id}/otp/send/")
        verify = signer.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")
        return signing_request, verify.json()["session_token"]

    def test_the_signer_is_shown_the_value_but_not_asked_for_it(self) -> None:
        self.prefill(self.landlord_field, "1,450.00")
        signing_request, _ = self.send_and_verify()

        signer = APIClient()
        signer.post(f"/api/sign/{signing_request.id}/otp/send/")
        signer.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")
        payload = signer.get(f"/api/sign/{signing_request.id}/").json()

        fields = {field["label"]: field for field in payload["fields"]}
        self.assertTrue(fields["Monthly rent"]["is_prefilled"])
        self.assertEqual(fields["Monthly rent"]["prefilled_value"], "1,450.00")
        self.assertFalse(fields["Tenant full name"]["is_prefilled"])

    def test_a_signer_cannot_change_what_the_sender_filled(self) -> None:
        self.prefill(self.landlord_field, "1,450.00")
        signing_request, session_token = self.send_and_verify()

        signer = APIClient()
        response = signer.post(
            f"/api/sign/{signing_request.id}/submit/",
            {
                "session_token": session_token,
                f"field_{self.landlord_field.id}_type": "TEXT",
                f"field_{self.landlord_field.id}_value": "1.00",
                f"field_{self.tenant_field.id}_type": "TEXT",
                f"field_{self.tenant_field.id}_value": "Avery Morgan",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(str(self.landlord_field.id), response.json()["field_errors"])

    def test_a_prefilled_field_is_not_required_of_the_signer(self) -> None:
        self.prefill(self.landlord_field, "1,450.00")
        self.signature_field.delete()
        signing_request, session_token = self.send_and_verify()

        signer = APIClient()
        with self.captureOnCommitCallbacks(execute=True):
            response = signer.post(
                f"/api/sign/{signing_request.id}/submit/",
                {
                    "session_token": session_token,
                    f"field_{self.tenant_field.id}_type": "TEXT",
                    f"field_{self.tenant_field.id}_value": "Avery Morgan",
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, 200, response.content)
        signing_request.refresh_from_db()
        self.assertEqual(signing_request.status, SigningRequest.StatusEnum.SIGNED)
        # The sender's answer is not a submission; nobody submitted it.
        self.assertEqual(FieldSubmission.objects.filter(signing_request=signing_request).count(), 1)

    # ------------------------------------------------------------ what ends up on the document

    def test_the_senders_answer_is_on_the_completed_copy(self) -> None:
        """Without this the agreement arrives with its own terms missing."""
        self.prefill(self.landlord_field, "1450.00")
        self.signature_field.delete()
        signing_request, session_token = self.send_and_verify()

        signer = APIClient()
        with self.captureOnCommitCallbacks(execute=True):
            signer.post(
                f"/api/sign/{signing_request.id}/submit/",
                {
                    "session_token": session_token,
                    f"field_{self.tenant_field.id}_type": "TEXT",
                    f"field_{self.tenant_field.id}_value": "Avery Morgan",
                },
                format="multipart",
            )

        signing_request.refresh_from_db()
        self.assertTrue(signing_request.signed_pdf)
        with temporary_plaintext_file(signing_request.signed_pdf, suffix=".pdf") as path:
            with fitz.open(path) as pdf:
                text = pdf[0].get_text()
        self.assertIn("1450.00", text)
        self.assertIn("Avery Morgan", text)
