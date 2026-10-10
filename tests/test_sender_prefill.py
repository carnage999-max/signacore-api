"""Filling in your own part of an agreement before you send it.

Most of a contract is the sender's to complete - the property, the dates, the amounts. Asking
the signer for it is both work they cannot do and an invitation to get it wrong. These tests
cover what the sender may fill, what they may never fill, and that what they filled survives
into the document everybody ends up holding.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

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
from utils.file_storage import ENCRYPTED_FILE_HEADER, temporary_plaintext_file

from .test_admin_documents import build_flat_pdf
from .test_signing_flow import build_png_pixel

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
        self.landlord_field = self.make_field(
            DocumentField.FieldTypeEnum.TEXT,
            "Monthly rent",
            order=1,
            y=620,
            owner=DocumentField.AssignedToEnum.SENDER,
        )
        self.tenant_field = self.make_field(DocumentField.FieldTypeEnum.TEXT, "Tenant full name", order=2, y=560)
        self.signature_field = self.make_field(
            DocumentField.FieldTypeEnum.SIGNATURE, "Tenant signature", order=3, y=500, height=40
        )
        self.as_workspace()

    def make_field(self, field_type, label, *, order, y, height=24, owner=None) -> DocumentField:
        return DocumentField.objects.create(
            assigned_to=owner or DocumentField.AssignedToEnum.SIGNER,
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

    def answer(self, field: DocumentField, value: str):
        return self.client.patch(
            f"/api/admin/documents/{self.document.id}/fields/{field.id}/",
            {"sender_value": value},
            format="json",
        )

    # ------------------------------------------------------------------ what a sender may fill

    def test_the_sender_can_answer_their_own_part(self) -> None:
        response = self.answer(self.landlord_field, "1,450.00")

        self.assertEqual(response.status_code, 200, response.content)
        self.landlord_field.refresh_from_db()
        self.assertEqual(self.landlord_field.sender_value, "1,450.00")
        self.assertTrue(self.landlord_field.is_filled_by_sender)

    def test_who_filled_it_and_when_is_recorded(self) -> None:
        """A value on an agreement that nobody is named against is what a dispute turns on."""
        before = timezone.now()
        self.answer(self.landlord_field, "1,450.00")

        self.landlord_field.refresh_from_db()
        self.assertEqual(self.landlord_field.sender_filled_by, self.user)
        self.assertGreaterEqual(self.landlord_field.sender_filled_at, before)

    def test_clearing_a_value_clears_who_filled_it(self) -> None:
        self.answer(self.landlord_field, "1,450.00")
        self.answer(self.landlord_field, "")

        self.landlord_field.refresh_from_db()
        self.assertFalse(self.landlord_field.is_filled_by_sender)
        self.assertIsNone(self.landlord_field.sender_filled_by)
        self.assertIsNone(self.landlord_field.sender_filled_at)

    # ------------------------------------------------------------- what a sender may never fill

    def test_a_sender_cannot_answer_a_field_that_is_the_signers(self) -> None:
        """The line the whole product rests on, and it is about whose field it is.

        Not about what type the field is: a sender signing their own signature field on their
        own contract is ordinary and necessary. What must never happen is one party producing
        the other party's mark.
        """
        response = self.answer(self.tenant_field, "Avery Morgan")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("sender_value", response.json())
        self.tenant_field.refresh_from_db()
        self.assertFalse(self.tenant_field.is_filled_by_sender)

    def test_a_sender_cannot_sign_a_signature_field_that_is_the_signers(self) -> None:
        response = self.client.post(
            f"/api/admin/documents/{self.document.id}/fields/{self.signature_field.id}/signature/",
            {"signature": SimpleUploadedFile("sig.png", build_png_pixel(), content_type="image/png")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.signature_field.refresh_from_db()
        self.assertFalse(self.signature_field.sender_signature)

    def test_a_field_cannot_be_reassigned_and_answered_in_one_request(self) -> None:
        """A check reading only the stored owner would let this through."""
        response = self.client.patch(
            f"/api/admin/documents/{self.document.id}/fields/{self.tenant_field.id}/",
            {"assigned_to": "SIGNER", "sender_value": "Avery Morgan"},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.tenant_field.refresh_from_db()
        self.assertFalse(self.tenant_field.is_filled_by_sender)

    # ---------------------------------------------------- the sender signing their own contract

    def landlord_signature_field(self) -> DocumentField:
        return self.make_field(
            DocumentField.FieldTypeEnum.SIGNATURE,
            "Landlord signature",
            order=5,
            y=420,
            height=40,
            owner=DocumentField.AssignedToEnum.SENDER,
        )

    def sign_as_sender(self, field: DocumentField):
        return self.client.post(
            f"/api/admin/documents/{self.document.id}/fields/{field.id}/signature/",
            {"signature": SimpleUploadedFile("sig.png", build_png_pixel(), content_type="image/png")},
            format="multipart",
            HTTP_X_REAL_IP="203.0.113.9",
        )

    def test_the_sender_can_sign_their_own_signature_field(self) -> None:
        """A lease has a landlord's signature as well as a tenant's."""
        field = self.landlord_signature_field()

        response = self.sign_as_sender(field)

        self.assertEqual(response.status_code, 200, response.content)
        field.refresh_from_db()
        self.assertTrue(field.sender_signature)
        self.assertTrue(field.is_filled_by_sender)

    def test_a_senders_signature_is_recorded_like_a_signature(self) -> None:
        """It carries the same weight as the other party's, so it is recorded the same way."""
        field = self.landlord_signature_field()
        before = timezone.now()

        self.sign_as_sender(field)

        field.refresh_from_db()
        self.assertEqual(field.sender_filled_by, self.user)
        self.assertGreaterEqual(field.sender_filled_at, before)
        self.assertEqual(field.sender_filled_ip, "203.0.113.9")

    def test_a_senders_signature_is_stored_as_ciphertext(self) -> None:
        field = self.landlord_signature_field()
        self.sign_as_sender(field)

        field.refresh_from_db()
        stored = (Path(TEST_MEDIA_ROOT) / field.sender_signature.name).read_bytes()
        self.assertTrue(stored.startswith(ENCRYPTED_FILE_HEADER))
        self.assertNotIn(build_png_pixel(), stored)

    def test_the_sender_cannot_sign_a_field_that_is_not_a_signature(self) -> None:
        response = self.sign_as_sender(self.landlord_field)

        self.assertEqual(response.status_code, 400, response.content)

    def test_handing_a_field_back_takes_the_senders_mark_with_it(self) -> None:
        """Otherwise one party's signature is left sitting on a field the other is now asked for."""
        field = self.landlord_signature_field()
        self.sign_as_sender(field)

        response = self.client.patch(
            f"/api/admin/documents/{self.document.id}/fields/{field.id}/",
            {"assigned_to": "SIGNER"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        field.refresh_from_db()
        self.assertFalse(field.sender_signature)
        self.assertFalse(field.is_filled_by_sender)
        self.assertIsNone(field.sender_filled_by)

    def test_the_senders_signature_is_drawn_on_every_copy(self) -> None:
        field = self.landlord_signature_field()
        self.sign_as_sender(field)
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
        with temporary_plaintext_file(signing_request.signed_pdf, suffix=".pdf") as path:
            with fitz.open(path) as pdf:
                self.assertTrue(pdf[0].get_images(), "the sender's signature should be on the page")

    # --------------------------------------------------------------- what the signer then sees

    def send_and_verify(self) -> tuple[SigningRequest, str]:
        # A sender finishes their own fields before sending; the service now insists on it, so
        # anything still blank is filled here rather than each test remembering to.
        for field in self.document.fields.filter(assigned_to=DocumentField.AssignedToEnum.SENDER):
            if field.is_required and not field.is_filled_by_sender:
                self.answer(field, "1,450.00")
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
        self.answer(self.landlord_field, "1,450.00")
        signing_request, _ = self.send_and_verify()

        signer = APIClient()
        signer.post(f"/api/sign/{signing_request.id}/otp/send/")
        signer.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")
        payload = signer.get(f"/api/sign/{signing_request.id}/").json()

        fields = {field["label"]: field for field in payload["fields"]}
        self.assertTrue(fields["Monthly rent"]["is_filled_by_sender"])
        self.assertEqual(fields["Monthly rent"]["sender_value"], "1,450.00")
        self.assertFalse(fields["Tenant full name"]["is_filled_by_sender"])

    def test_a_signer_cannot_change_what_the_sender_filled(self) -> None:
        self.answer(self.landlord_field, "1,450.00")
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
        self.answer(self.landlord_field, "1,450.00")
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
        self.answer(self.landlord_field, "1450.00")
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

    def test_a_sender_field_left_blank_is_still_not_the_signers(self) -> None:
        """Ownership decides, not whether it happens to carry a value."""
        blank = self.make_field(
            DocumentField.FieldTypeEnum.TEXT,
            "Agent reference",
            order=6,
            y=380,
            owner=DocumentField.AssignedToEnum.SENDER,
        )
        blank.is_required = False
        blank.save(update_fields=["is_required"])
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

    def test_a_document_cannot_go_out_with_the_senders_own_fields_blank(self) -> None:
        """A lease must not reach a tenant with the rent missing and nobody to ask."""
        self.answer(self.landlord_field, "")

        response = self.client.post(
            f"/api/admin/documents/{self.document.id}/send/",
            {"signers": [{"signer_email": "tenant@example.com", "signer_name": "Avery Morgan"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Monthly rent", response.json()["fields"][0])
        self.assertFalse(self.document.signing_requests.exists())

    def test_the_signer_can_see_the_mark_the_sender_already_made(self) -> None:
        """Somebody agreeing to a countersigned contract should see that it is countersigned."""
        field = self.landlord_signature_field()
        self.sign_as_sender(field)
        signing_request, _ = self.send_and_verify()

        signer = APIClient()
        signer.post(f"/api/sign/{signing_request.id}/otp/send/")
        signer.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")
        response = signer.get(f"/api/sign/{signing_request.id}/fields/{field.id}/sender-signature/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response.content, build_png_pixel())

    def test_an_unverified_signer_cannot_see_it(self) -> None:
        field = self.landlord_signature_field()
        self.sign_as_sender(field)
        signing_request, _ = self.send_and_verify()

        response = APIClient().get(f"/api/sign/{signing_request.id}/fields/{field.id}/sender-signature/")

        self.assertEqual(response.status_code, 403)

    def test_nothing_here_reaches_another_signers_signature(self) -> None:
        """The route only ever serves a field the sender owns."""
        signing_request, session_token = self.send_and_verify()
        signer = APIClient()
        signer.post(f"/api/sign/{signing_request.id}/otp/send/")
        signer.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")

        response = signer.get(f"/api/sign/{signing_request.id}/fields/{self.signature_field.id}/sender-signature/")

        self.assertEqual(response.status_code, 404)
