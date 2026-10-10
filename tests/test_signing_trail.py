"""The history behind a signature.

A signature is only as useful in a dispute as the record of how it came to exist. These tests
cover what that record contains, that it survives the things that used to erase it, and that it
is only ever added to.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import AccountProfile, Organization, OrganizationMembership
from apps.documents.models import Document, DocumentField
from apps.signing.events import OPEN_DEDUPE_MINUTES, record_signing_event
from apps.signing.models import SigningEvent, SigningRequest

from .test_admin_documents import build_flat_pdf

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="signacore-trail-media-")


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_TEST_OTP_CODE="123456",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
)
class SigningTrailTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="trail-admin",
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
        self.organization = Organization.objects.create(name="Trail Company", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        self.document = Document.objects.create(
            title="Contractor Agreement",
            original_pdf=SimpleUploadedFile("deal.pdf", build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
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
            signer_email="avery@example.com",
            signer_name="Avery Morgan",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def events(self) -> list[str]:
        return list(self.signing_request.events.order_by("at", "created_at").values_list("event", flat=True))

    def verify(self, ip: str = "81.2.44.10") -> str:
        self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/", HTTP_X_REAL_IP=ip)
        response = self.client.post(
            f"/api/sign/{self.signing_request.id}/otp/verify/",
            {"otp": "123456"},
            format="json",
            HTTP_X_REAL_IP=ip,
        )
        return response.json()["session_token"]

    def submit(self, session_token: str, ip: str = "81.2.44.10") -> None:
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/sign/{self.signing_request.id}/submit/",
                {
                    "session_token": session_token,
                    f"field_{self.field.id}_type": "TEXT",
                    f"field_{self.field.id}_value": "Avery Morgan",
                },
                format="multipart",
                HTTP_X_REAL_IP=ip,
            )
        self.assertEqual(response.status_code, 200, response.content)

    def sign(self, ip: str = "81.2.44.10") -> None:
        self.submit(self.verify(ip), ip)

    def as_workspace(self) -> None:
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )

    def test_the_whole_journey_is_recorded_in_order(self) -> None:
        self.client.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP="81.2.44.10")
        self.sign()

        self.assertEqual(
            self.events(),
            ["OPENED", "CODE_SENT", "CODE_VERIFIED", "SIGNED", "COPY_DELIVERED"],
        )

    def test_opening_the_link_is_recorded_with_where_from(self) -> None:
        self.client.get(
            f"/api/sign/{self.signing_request.id}/",
            HTTP_X_REAL_IP="203.0.113.7",
            HTTP_USER_AGENT="Mozilla/5.0 (iPhone)",
        )

        opened = self.signing_request.events.get(event=SigningEvent.EventEnum.OPENED)
        self.assertEqual(opened.ip_address, "203.0.113.7")
        self.assertEqual(opened.user_agent, "Mozilla/5.0 (iPhone)")

    def test_a_refresh_does_not_become_a_second_opening(self) -> None:
        """A preview pane or a link scanner should not be able to inflate somebody's history."""
        for _ in range(5):
            self.client.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP="203.0.113.7")

        self.assertEqual(self.signing_request.events.filter(event=SigningEvent.EventEnum.OPENED).count(), 1)

    def test_opening_from_somewhere_else_is_a_separate_occasion(self) -> None:
        self.client.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP="203.0.113.7")
        self.client.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP="198.51.100.4")

        addresses = {
            event.ip_address for event in self.signing_request.events.filter(event=SigningEvent.EventEnum.OPENED)
        }
        self.assertEqual(addresses, {"203.0.113.7", "198.51.100.4"})

    def test_an_opening_after_the_window_is_recorded_again(self) -> None:
        self.client.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP="203.0.113.7")
        stale = timezone.now() - timedelta(minutes=OPEN_DEDUPE_MINUTES + 1)
        self.signing_request.events.update(at=stale)

        self.client.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP="203.0.113.7")

        self.assertEqual(self.signing_request.events.filter(event=SigningEvent.EventEnum.OPENED).count(), 2)

    def test_verifying_and_signing_keep_their_own_addresses(self) -> None:
        """The thing SigningRequest could not express: a signer who moved between networks."""
        self.submit(self.verify("203.0.113.7"), ip="198.51.100.4")

        verified = self.signing_request.events.get(event=SigningEvent.EventEnum.CODE_VERIFIED)
        signed = self.signing_request.events.get(event=SigningEvent.EventEnum.SIGNED)
        self.assertEqual(verified.ip_address, "203.0.113.7")
        self.assertEqual(signed.ip_address, "198.51.100.4")
        # The column on the request keeps only the later of the two, which is why it was never
        # enough on its own.
        self.signing_request.refresh_from_db()
        self.assertEqual(self.signing_request.ip_address, "198.51.100.4")

    def test_sending_records_who_sent_it(self) -> None:
        self.as_workspace()
        response = self.client.post(
            f"/api/admin/documents/{self.document.id}/send/",
            {"signers": [{"signer_email": "jordan@example.com", "signer_name": "Jordan Reyes"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        new_request = self.document.signing_requests.exclude(pk=self.signing_request.pk).get()
        sent = new_request.events.get(event=SigningEvent.EventEnum.SENT)
        self.assertIn("Priya Shah", sent.detail)

    def test_reopening_does_not_erase_that_it_was_signed(self) -> None:
        """Reopening clears signed_at, the address and the submissions. It must not clear this."""
        self.sign()
        self.signing_request.refresh_from_db()
        self.assertIsNotNone(self.signing_request.signed_at)

        self.as_workspace()
        response = self.client.post(
            f"/api/admin/documents/{self.document.id}/signing-requests/{self.signing_request.id}/resend/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        self.signing_request.refresh_from_db()
        self.assertIsNone(self.signing_request.signed_at)
        self.assertEqual(self.signing_request.ip_address, "")
        # The columns were cleared; the history was not.
        self.assertIn("SIGNED", self.events())
        reopened = self.signing_request.events.get(event=SigningEvent.EventEnum.REOPENED)
        self.assertIn("discarding a completed signature", reopened.detail)
        self.assertEqual(self.events()[-1], "SENT")

    def test_the_workspace_can_read_the_trail(self) -> None:
        self.client.get(f"/api/sign/{self.signing_request.id}/", HTTP_X_REAL_IP="203.0.113.7")
        self.sign()

        self.as_workspace()
        response = self.client.get(f"/api/admin/documents/{self.document.id}/")
        self.assertEqual(response.status_code, 200, response.content)
        signer = response.json()["signing_requests"][0]
        labels = [event["label"] for event in signer["events"]]
        self.assertIn("Document opened", labels)
        self.assertIn("Email verified", labels)
        self.assertIn("Signed and submitted", labels)
        self.assertTrue(all(event["at"] for event in signer["events"]))

    def test_an_event_is_never_rewritten(self) -> None:
        """Nothing in the product updates one. The test states it so a future change has to argue."""
        record_signing_event(self.signing_request, SigningEvent.EventEnum.SENT)
        first = self.signing_request.events.get()
        record_signing_event(self.signing_request, SigningEvent.EventEnum.SENT)

        self.assertEqual(self.signing_request.events.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.event, SigningEvent.EventEnum.SENT)

    def test_a_changed_device_cannot_pad_the_history(self) -> None:
        """The open endpoint is unauthenticated by necessity - a signer has no account.

        Keying the window on the address alone is what stops anybody holding the link from
        writing a row per request simply by varying the user agent.
        """
        for index in range(6):
            self.client.get(
                f"/api/sign/{self.signing_request.id}/",
                HTTP_X_REAL_IP="203.0.113.7",
                HTTP_USER_AGENT=f"Mozilla/5.0 (Device {index})",
            )

        self.assertEqual(self.signing_request.events.filter(event=SigningEvent.EventEnum.OPENED).count(), 1)
