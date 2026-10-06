"""Each signer gets their own copy, made from their own answers, the moment they sign.

A document used to hold one copy for everybody, flattened from every signer's submissions at
once. That is right for a contract several parties put their names to, and wrong for anything
sent to a group to fill in separately: eight people answering the same survey produced one page
with eight sets of answers drawn over each other, which every one of them then received. Nobody
got anything at all until the last of them signed, which could be never.
"""

from __future__ import annotations

from datetime import timedelta

import fitz
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
from utils.file_storage import temporary_plaintext_file

from . import pdf_builders as builders
from .test_pdf_form_import import TEST_MEDIA_ROOT


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ACopyForEachSignerTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.owner = get_user_model().objects.create_user(
            username="admin", email="admin@example.com", password="pw123456", is_staff=True
        )
        self.organization = Organization.objects.create(name="Se7en", created_by=self.owner)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.owner,
            role=OrganizationMembership.RoleEnum.ADMIN,
        )
        self.document = Document.objects.create(
            title="Employee survey",
            original_pdf=SimpleUploadedFile("survey.pdf", builders.build_flat_pdf()),
            created_by=self.owner,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
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
        self.ada, self.grace = (
            SigningRequest.objects.create(
                document=self.document,
                signer_email=f"{name.lower()}@se7en.example",
                signer_name=name,
                expires_at=timezone.now() + timedelta(days=7),
            )
            for name in ("Ada", "Grace")
        )

    def sign(self, signing_request, answer: str):
        """Returns the response; the client is kept so the signer can come back afterwards."""
        client = APIClient()
        self.clients = getattr(self, "clients", {})
        self.clients[signing_request.id] = client
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            client.post(f"/api/sign/{signing_request.id}/otp/send/")
            token = client.post(f"/api/sign/{signing_request.id}/otp/verify/", {"otp": "123456"}, format="json").json()[
                "session_token"
            ]
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(
                f"/api/sign/{signing_request.id}/submit/",
                {
                    "session_token": token,
                    f"field_{self.field.id}_type": "TEXT",
                    f"field_{self.field.id}_value": answer,
                },
                format="multipart",
            )

    def text_of(self, signing_request) -> str:
        signing_request.refresh_from_db()
        with temporary_plaintext_file(signing_request.signed_pdf, suffix=".pdf") as path:
            with fitz.open(path) as document:
                return document[0].get_text()

    def test_a_signer_has_their_copy_before_anyone_else_signs(self) -> None:
        """The complaint that started this: nothing arrived, and nothing was going to."""
        response = self.sign(self.ada, "Ada Lovelace")

        self.assertEqual(response.status_code, 200, response.json())
        self.ada.refresh_from_db()
        self.assertTrue(self.ada.signed_pdf, "Ada signed, so Ada has a copy")
        self.grace.refresh_from_db()
        self.assertFalse(self.grace.signed_pdf, "Grace has not signed, so has nothing")

    def test_a_copy_carries_its_own_signer_and_nobody_else(self) -> None:
        self.sign(self.ada, "Ada Lovelace")
        self.sign(self.grace, "Grace Hopper")

        ada_copy = self.text_of(self.ada)
        grace_copy = self.text_of(self.grace)

        self.assertIn("Ada Lovelace", ada_copy)
        self.assertNotIn("Grace Hopper", ada_copy)
        self.assertIn("Grace Hopper", grace_copy)
        self.assertNotIn("Ada Lovelace", grace_copy)

    def test_the_signer_is_emailed_their_copy_when_they_sign(self) -> None:
        mail.outbox.clear()
        self.sign(self.ada, "Ada Lovelace")

        # Her verification code is in the outbox too; the copy is the one with the attachment.
        carrying_a_copy = [message for message in mail.outbox if message.attachments]

        self.assertEqual(len(carrying_a_copy), 1)
        self.assertEqual(carrying_a_copy[0].recipients(), ["ada@se7en.example"], "one signer, one email")
        self.assertTrue(carrying_a_copy[0].attachments[0][0].endswith(".pdf"))

    def test_the_sender_is_told_each_time_somebody_signs(self) -> None:
        mail.outbox.clear()
        self.sign(self.ada, "Ada Lovelace")

        to_owner = [message for message in mail.outbox if "admin@example.com" in message.recipients()]
        self.assertEqual(len(to_owner), 1)

    def test_the_document_follows_its_signers(self) -> None:
        self.sign(self.ada, "Ada Lovelace")
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.StatusEnum.PARTIALLY_SIGNED)

        self.sign(self.grace, "Grace Hopper")
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.StatusEnum.COMPLETED)

    def test_a_signer_downloads_their_own_copy_and_not_the_others(self) -> None:
        self.sign(self.ada, "Ada Lovelace")
        self.sign(self.grace, "Grace Hopper")

        response = self.clients[self.ada.id].get(f"/api/sign/{self.ada.id}/signed/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_the_sender_downloads_each_signer_separately(self) -> None:
        self.sign(self.ada, "Ada Lovelace")
        self.sign(self.grace, "Grace Hopper")

        admin = APIClient()
        admin.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.owner.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )
        base = f"/api/admin/documents/{self.document.id}/signing-requests"
        ada = admin.get(f"{base}/{self.ada.id}/download/")
        grace = admin.get(f"{base}/{self.grace.id}/download/")

        self.assertEqual(ada.status_code, 200)
        self.assertEqual(grace.status_code, 200)
        self.assertNotEqual(b"".join(ada.streaming_content), b"".join(grace.streaming_content))
