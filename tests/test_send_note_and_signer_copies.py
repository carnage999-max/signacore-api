"""A note from the sender, and each signer's copy being fetchable the moment it exists.

Two things the workspace could not do. There was nowhere to say what a document was for, so every
recipient got the same standing sentence and nothing else. And although a signer receives their
copy the instant they submit, the workspace could not fetch that copy until every other recipient
had finished - which for a survey sent to eight people could be never.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Organization, OrganizationMembership
from apps.documents.models import Document, DocumentField
from apps.signing.models import SigningRequest

from . import pdf_builders as builders
from .test_pdf_form_import import TEST_MEDIA_ROOT


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class SendNoteAndSignerCopyTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.owner = get_user_model().objects.create_user(
            username="owner", email="owner@example.com", password="pw123456", is_staff=True
        )
        self.organization = Organization.objects.create(name="Example Ltd", created_by=self.owner)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationMembership.RoleEnum.ADMIN,
        )
        self.document = Document.objects.create(
            title="Survey Document",
            original_pdf=SimpleUploadedFile("survey.pdf", builders.build_flat_pdf()),
            created_by=self.owner,
            organization=self.organization,
            status=Document.StatusEnum.DRAFT,
        )
        self.field = DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Full name",
            page=1,
            x=72,
            y=620,
            width=220,
            height=24,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )
        self.admin = APIClient()
        self.admin.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.owner.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def send(self, *, message=None, email="avery@example.com"):
        payload = {"signers": [{"signer_name": "Avery", "signer_email": email}]}
        if message is not None:
            payload["message"] = message
        return self.admin.post(f"/api/admin/documents/{self.document.id}/send/", payload, format="json")

    def sign(self, signing_request: SigningRequest) -> APIClient:
        client = APIClient()
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            client.post(f"/api/sign/{signing_request.id}/otp/send/")
            token = client.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json").json()[
                "session_token"
            ]
        with self.captureOnCommitCallbacks(execute=True):
            client.post(
                f"/api/sign/{signing_request.id}/submit/",
                {
                    "session_token": token,
                    f"field_{self.field.id}_type": "TEXT",
                    f"field_{self.field.id}_value": "Avery",
                },
                format="multipart",
            )
        return client

    # ------------------------------------------------------------------------------- the note

    def test_the_note_reaches_the_recipient_in_their_invitation(self) -> None:
        note = "Please return this before Friday. Questions to payroll."

        response = self.send(message=note)

        self.assertEqual(response.status_code, 200, response.json())
        self.document.refresh_from_db()
        self.assertEqual(self.document.send_message, note)
        invitation = mail.outbox[-1]
        self.assertIn(note, invitation.body)
        self.assertIn(note, invitation.alternatives[0][0])

    def test_sending_without_a_note_reads_exactly_as_it_did_before(self) -> None:
        response = self.send()

        self.assertEqual(response.status_code, 200, response.json())
        self.document.refresh_from_db()
        self.assertEqual(self.document.send_message, "")
        self.assertIn("Please review and complete this document", mail.outbox[-1].alternatives[0][0])

    def test_a_note_cannot_carry_markup_into_the_email(self) -> None:
        """It is somebody's typing, rendered in somebody else's mail client."""
        self.send(message='<img src=x onerror="alert(1)">')

        html = mail.outbox[-1].alternatives[0][0]
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)

    def test_a_note_written_as_two_paragraphs_arrives_as_two(self) -> None:
        self.send(message="First line.\n\nSecond line.")

        html = mail.outbox[-1].alternatives[0][0]
        self.assertIn("First line.<br><br>Second line.", html)

    def test_a_resend_keeps_the_note_the_first_send_carried(self) -> None:
        self.send(message="Due Friday.")
        self.send(email="second@example.com")

        self.document.refresh_from_db()
        self.assertEqual(self.document.send_message, "Due Friday.")

    def test_a_note_longer_than_a_note_is_refused(self) -> None:
        response = self.send(message="x" * 2001)

        self.assertEqual(response.status_code, 400)
        self.assertIn("message", response.json())

    # --------------------------------------------------------------- each signer's copy, as soon

    def test_the_workspace_can_fetch_a_signer_copy_before_everyone_else_has_signed(self) -> None:
        """The reported bug. One of eight signs; their copy exists; it must be fetchable."""
        self.send()
        self.send(email="second@example.com")
        first, second = SigningRequest.objects.order_by("created_at")
        self.sign(first)

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.StatusEnum.PARTIALLY_SIGNED)

        downloaded = self.admin.get(f"/api/admin/documents/{self.document.id}/signing-requests/{first.id}/download/")

        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded["Content-Type"], "application/pdf")
        self.assertEqual(
            self.admin.get(
                f"/api/admin/documents/{self.document.id}/signing-requests/{second.id}/download/"
            ).status_code,
            404,
            "a signer who has not signed has no copy to fetch",
        )

    def test_the_workspace_is_told_which_rows_have_a_copy(self) -> None:
        """Without this the page cannot know which download buttons mean anything."""
        self.send()
        self.send(email="second@example.com")
        first, _second = SigningRequest.objects.order_by("created_at")
        self.sign(first)

        detail = self.admin.get(f"/api/admin/documents/{self.document.id}/").json()

        by_id = {row["id"]: row for row in detail["signing_requests"]}
        self.assertTrue(by_id[str(first.id)]["signed_copy_ready"])
        self.assertFalse(all(row["signed_copy_ready"] for row in detail["signing_requests"]))

    def test_one_signers_copy_is_not_another_signers(self) -> None:
        self.send()
        self.send(email="second@example.com")
        first, second = SigningRequest.objects.order_by("created_at")
        self.sign(first)
        self.sign(second)

        first_copy = b"".join(
            self.admin.get(
                f"/api/admin/documents/{self.document.id}/signing-requests/{first.id}/download/"
            ).streaming_content
        )
        second_copy = b"".join(
            self.admin.get(
                f"/api/admin/documents/{self.document.id}/signing-requests/{second.id}/download/"
            ).streaming_content
        )

        self.assertNotEqual(first_copy, second_copy)

    def test_another_workspace_cannot_fetch_a_signer_copy(self) -> None:
        self.send()
        signing_request = SigningRequest.objects.get()
        self.sign(signing_request)

        intruder_user = get_user_model().objects.create_user(
            username="intruder", email="intruder@example.com", password="pw123456", is_staff=True
        )
        intruder_organization = Organization.objects.create(name="Other Ltd", created_by=intruder_user)
        OrganizationMembership.objects.create(
            organization=intruder_organization,
            user=intruder_user,
            role=OrganizationMembership.RoleEnum.ADMIN,
        )
        intruder = APIClient()
        intruder.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(intruder_user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(intruder_organization.id),
        )

        refused = intruder.get(
            f"/api/admin/documents/{self.document.id}/signing-requests/{signing_request.id}/download/"
        )

        self.assertEqual(refused.status_code, 404)

    # ------------------------------------------------------------------- what "in progress" means

    def test_verifying_the_code_records_when(self) -> None:
        """OTP_VERIFIED is kept for a week; a signing session lasts an hour. Only the time can
        tell the workspace whether somebody is signing now or opened it on Monday."""
        self.send()
        signing_request = SigningRequest.objects.get()
        self.assertIsNone(signing_request.otp_verified_at)

        client = APIClient()
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            client.post(f"/api/sign/{signing_request.id}/otp/send/")
            client.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")

        signing_request.refresh_from_db()
        self.assertIsNotNone(signing_request.otp_verified_at)
        self.assertLess(timezone.now() - signing_request.otp_verified_at, timedelta(seconds=30))

    def test_the_workspace_is_told_when_the_code_was_verified(self) -> None:
        self.send()
        signing_request = SigningRequest.objects.get()
        client = APIClient()
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            client.post(f"/api/sign/{signing_request.id}/otp/send/")
            client.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")

        detail = self.admin.get(f"/api/admin/documents/{self.document.id}/").json()

        self.assertIsNotNone(detail["signing_requests"][0]["otp_verified_at"])
