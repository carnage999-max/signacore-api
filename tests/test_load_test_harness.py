"""The seeding command has to produce sessions a running server will actually accept.

Everything expensive in the signer portal is behind the cookie issued after an emailed code, so
until this existed a load test could only reach the cheap pages. The tests that matter here are
that the minted cookie opens the door, and that cleaning up afterwards really removes everything -
a load test leaves hundreds of documents behind, and they are encrypted files on disk, not only
rows.
"""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Organization
from apps.documents.models import Document
from apps.signing.models import SigningRequest

from .test_pdf_form_import import TEST_MEDIA_ROOT

SESSION_COOKIE = "signacore_signer_session"


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT, SENTRY_ENVIRONMENT="staging")
class SeedLoadTestCommandTests(TestCase):
    def setUp(self) -> None:
        cache.clear()

    def seed(self, **options) -> list[dict[str, str]]:
        out = StringIO()
        call_command("seed_load_test", stdout=out, stderr=StringIO(), **options)
        rows = [line.split(",") for line in out.getvalue().strip().splitlines()]
        header, *body = rows
        return [dict(zip(header, row)) for row in body]

    def test_a_seeded_session_opens_the_pages_a_load_test_needs(self) -> None:
        """The whole point. Without this the harness cannot reach anything that costs."""
        row = self.seed(signers=1, pages=2)[0]
        client = APIClient()

        refused = client.get(f"/api/sign/{row['token']}/pages/1/preview/")
        self.assertEqual(refused.status_code, 403, "a preview must still need a session")

        client.cookies[SESSION_COOKIE] = row["session_cookie"]
        allowed = client.get(f"/api/sign/{row['token']}/pages/1/preview/")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed["Content-Type"], "image/png")

    def test_each_signer_gets_their_own_document(self) -> None:
        """Sharing one would make virtual users share a rate-limit bucket and throttle each other."""
        rows = self.seed(signers=3, pages=1)

        self.assertEqual(len({row["token"] for row in rows}), 3)
        self.assertEqual(Document.objects.count(), 3)

    def test_the_document_has_the_pages_that_were_asked_for(self) -> None:
        """Pages are the load dial, so the count has to be honoured."""
        row = self.seed(signers=1, pages=5)[0]
        client = APIClient()
        client.cookies[SESSION_COOKIE] = row["session_cookie"]

        context = client.get(f"/api/sign/{row['token']}/").json()

        self.assertEqual(row["pages"], "5")
        self.assertEqual(context["page_count"], 5)

    def test_cleaning_up_removes_the_rows_and_the_stored_files(self) -> None:
        """A run leaves hundreds of encrypted PDFs; the rows alone are not the whole mess."""
        self.seed(signers=2, pages=1)
        stored = [Document.objects.get(pk=pk).original_pdf for pk in Document.objects.values_list("pk", flat=True)]
        self.assertTrue(all(field.storage.exists(field.name) for field in stored))

        call_command("seed_load_test", clean=True, stdout=StringIO(), stderr=StringIO())

        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(SigningRequest.objects.count(), 0)
        self.assertEqual(Organization.objects.filter(name="Load test workspace").count(), 0)
        self.assertEqual(get_user_model().objects.filter(username="load-test-owner").count(), 0)
        for field in stored:
            self.assertFalse(field.storage.exists(field.name), "an encrypted PDF was left behind")

    def test_cleaning_an_empty_database_is_not_an_error(self) -> None:
        call_command("seed_load_test", clean=True, stdout=StringIO(), stderr=StringIO())

    @override_settings(SENTRY_ENVIRONMENT="production")
    def test_it_refuses_to_seed_production_without_being_told_twice(self) -> None:
        """Seeding writes documents wherever it is pointed. Beside real agreements is not where."""
        with self.assertRaises(CommandError) as refusal:
            call_command("seed_load_test", signers=1, stdout=StringIO(), stderr=StringIO())

        self.assertIn("production", str(refusal.exception))
        self.assertEqual(Document.objects.count(), 0)

    @override_settings(SENTRY_ENVIRONMENT="production")
    def test_production_can_be_insisted_on(self) -> None:
        rows = self.seed(signers=1, pages=1, allow_production=True)

        self.assertEqual(len(rows), 1)

    def test_it_will_not_seed_nothing(self) -> None:
        for options in ({"signers": 0}, {"pages": 0}):
            with self.subTest(**options):
                with self.assertRaises(CommandError):
                    call_command("seed_load_test", stdout=StringIO(), stderr=StringIO(), **options)
