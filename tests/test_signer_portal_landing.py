"""The signing host needs a door, not a 404.

sign.mysignacore.com answered a raw nginx 404 at its root - server version and all - and a bare
Django 404 at /sign/. That is a broken-looking front door on the one domain a signer is told to
trust, and the people who land there are usually holding a link their mail client truncated.
"""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings
from django.urls import reverse


@override_settings(
    SIGNING_LINK_EXPIRY_DAYS=7,
    OTP_EXPIRY_MINUTES=10,
    OTP_RESEND_COOLDOWN_SECONDS=60,
    SIGNACORE_APP_URL="https://mysignacore.com",
)
class SignerPortalLandingTests(SimpleTestCase):
    def test_the_signing_host_answers_at_its_door(self) -> None:
        response = self.client.get(reverse("signer-portal-landing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You need the link from your email")

    def test_it_says_what_the_real_limits_are(self) -> None:
        """Read from the settings they describe, so the page cannot disagree with the service."""
        response = self.client.get(reverse("signer-portal-landing"))

        self.assertContains(response, "7 days")
        self.assertContains(response, "10 minutes")
        self.assertContains(response, "60 seconds")

    @override_settings(SIGNING_LINK_EXPIRY_DAYS=3, OTP_EXPIRY_MINUTES=5)
    def test_changing_a_limit_changes_the_page(self) -> None:
        response = self.client.get(reverse("signer-portal-landing"))

        self.assertContains(response, "3 days")
        self.assertContains(response, "5 minutes")
        self.assertNotContains(response, "7 days")

    def test_it_offers_a_way_to_reach_a_person(self) -> None:
        response = self.client.get(reverse("signer-portal-landing"))

        self.assertContains(response, "info@mysignacore.com")
        self.assertContains(response, "https://mysignacore.com/help")

    def test_it_is_kept_out_of_search_results(self) -> None:
        """A page telling people what to do about a signing link should not rank for anything."""
        response = self.client.get(reverse("signer-portal-landing"))

        self.assertContains(response, "noindex")

    def test_it_offers_nowhere_to_sign_in(self) -> None:
        """There is no signer account, so the door must not send anybody looking for one.

        Asserted as the absence of a way in rather than the absence of a phrase: the copy says
        there is nothing to sign in to here, which is the right thing to say and contains the
        words a cruder check would ban.
        """
        response = self.client.get(reverse("signer-portal-landing"))

        body = response.content.decode()
        self.assertNotIn('href="/login', body)
        self.assertNotIn("/register", body)
        self.assertNotIn("<form", body)
        self.assertIn("no account to create", body)

    def test_a_document_link_still_reaches_the_portal_itself(self) -> None:
        """The landing route must not shadow the one that matters."""
        response = self.client.get("/sign/00000000-0000-0000-0000-000000000000/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review and sign")
