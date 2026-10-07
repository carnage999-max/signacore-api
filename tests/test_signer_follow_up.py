"""The one email SignaCore sends about itself, and the way out of it.

Every signed document reaches somebody who has never heard of SignaCore, and until now the email
carrying their copy spent that attention on nothing. The risk in spending it is the obvious one:
an email nobody asked for, sent again and again, costs the sending domain more than it earns. So
most of what follows is about restraint - once per person ever, never to somebody who has said no,
and nothing at all unless it has been switched on.
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

from apps.accounts.models import Organization
from apps.documents.models import Document
from apps.notifications.models import EmailSuppression
from apps.signing.models import SigningRequest
from tasks.marketing import send_signer_follow_ups
from utils.unsubscribe import build_unsubscribe_token

from . import pdf_builders as builders
from .test_pdf_form_import import TEST_MEDIA_ROOT


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SIGNACORE_SIGNER_FOLLOW_UP_ENABLED=True,
)
class SignerFollowUpTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.owner = get_user_model().objects.create_user(
            username="owner", email="owner@example.com", password="pw123456", is_staff=True
        )
        self.organization = Organization.objects.create(name="Example Ltd", created_by=self.owner)
        self.document = Document.objects.create(
            title="Policy",
            original_pdf=SimpleUploadedFile("policy.pdf", builders.build_flat_pdf()),
            created_by=self.owner,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
        )

    def signed_request(self, *, email="avery@example.com", hours_ago=30) -> SigningRequest:
        request = SigningRequest.objects.create(
            document=self.document,
            signer_name="Avery",
            signer_email=email,
            status=SigningRequest.StatusEnum.SIGNED,
        )
        SigningRequest.objects.filter(pk=request.pk).update(signed_at=timezone.now() - timedelta(hours=hours_ago))
        request.refresh_from_db()
        return request

    # ------------------------------------------------------------------------------ restraint

    def test_nothing_is_sent_while_the_follow_up_is_switched_off(self) -> None:
        """The default. Outbound mail nobody is expecting waits for somebody to say go."""
        self.signed_request()

        with self.settings(SIGNACORE_SIGNER_FOLLOW_UP_ENABLED=False):
            sent = send_signer_follow_ups()

        self.assertEqual(sent, 0)
        self.assertEqual(mail.outbox, [])

    def test_somebody_who_signs_twice_hears_from_us_once(self) -> None:
        first = self.signed_request(hours_ago=40)
        second_document = Document.objects.create(
            title="Another",
            original_pdf=SimpleUploadedFile("another.pdf", builders.build_flat_pdf()),
            created_by=self.owner,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
        )
        second = SigningRequest.objects.create(
            document=second_document,
            signer_name="Avery",
            signer_email="avery@example.com",
            status=SigningRequest.StatusEnum.SIGNED,
        )
        SigningRequest.objects.filter(pk=second.pk).update(signed_at=timezone.now() - timedelta(hours=30))

        send_signer_follow_ups()

        self.assertEqual(len(mail.outbox), 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(first.follow_up_sent_at)
        self.assertIsNotNone(second.follow_up_sent_at, "the second must be marked, or it is found forever")

    def test_running_twice_does_not_send_twice(self) -> None:
        self.signed_request()

        send_signer_follow_ups()
        send_signer_follow_ups()

        self.assertEqual(len(mail.outbox), 1)

    def test_an_address_that_has_unsubscribed_is_not_written_to(self) -> None:
        EmailSuppression.suppress("avery@example.com")
        self.signed_request()

        sent = send_signer_follow_ups()

        self.assertEqual(sent, 0)
        self.assertEqual(mail.outbox, [])

    def test_a_signature_from_an_hour_ago_is_too_soon(self) -> None:
        self.signed_request(hours_ago=1)

        send_signer_follow_ups()

        self.assertEqual(mail.outbox, [])

    def test_a_signature_from_last_week_is_too_late(self) -> None:
        """An email about something somebody did last week reads as a list, not a follow-up."""
        self.signed_request(hours_ago=24 * 9)

        send_signer_follow_ups()

        self.assertEqual(mail.outbox, [])

    def test_somebody_who_has_not_signed_is_never_written_to(self) -> None:
        SigningRequest.objects.create(
            document=self.document,
            signer_name="Unsigned",
            signer_email="pending@example.com",
            status=SigningRequest.StatusEnum.PENDING,
        )

        send_signer_follow_ups()

        self.assertEqual(mail.outbox, [])

    # ------------------------------------------------------------------------------- what is sent

    def test_the_follow_up_says_what_it_is_and_how_to_leave(self) -> None:
        self.signed_request()

        send_signer_follow_ups()

        message = mail.outbox[0]
        self.assertIn("SignaCore", message.subject)
        self.assertIn("unsubscribe", message.body.lower())
        self.assertIn("List-Unsubscribe", message.extra_headers)
        self.assertIn("unsubscribe", message.extra_headers["List-Unsubscribe"].lower())

    # ------------------------------------------------------------------------------ the way out

    def test_the_unsubscribe_link_stops_the_emails(self) -> None:
        token = build_unsubscribe_token("avery@example.com")
        client = APIClient()

        confirmed = client.post("/api/notifications/unsubscribe/", {"token": token}, format="json")

        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(EmailSuppression.is_suppressed("avery@example.com"))

    def test_a_link_we_did_not_issue_does_nothing(self) -> None:
        client = APIClient()

        refused = client.post("/api/notifications/unsubscribe/", {"token": "someone@example.com:forged"}, format="json")

        self.assertEqual(refused.status_code, 400)
        self.assertFalse(EmailSuppression.objects.exists())

    def test_fetching_the_link_does_not_unsubscribe_anybody(self) -> None:
        """Mail clients prefetch links. A GET must not act."""
        token = build_unsubscribe_token("avery@example.com")
        client = APIClient()

        looked = client.get(f"/api/notifications/unsubscribe/?token={token}")

        self.assertEqual(looked.status_code, 200)
        self.assertFalse(EmailSuppression.is_suppressed("avery@example.com"))

    def test_unsubscribing_never_echoes_the_address_back(self) -> None:
        """The link lives in an email that may have been forwarded."""
        token = build_unsubscribe_token("avery@example.com")

        response = APIClient().post("/api/notifications/unsubscribe/", {"token": token}, format="json")

        self.assertNotIn("avery@example.com", str(response.json()))
