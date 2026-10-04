"""A rate limit should bound what one person can do, not what one office can.

The signer limits were keyed on the caller's address and nothing else, so the allowance belonged
to the network. An employment pack sent to several people at one company is signed from one
office, behind one address, and those signers took each other's budget - and a throttled page
preview arrives as a broken image, so the document simply stops rendering with nothing said.

The same keying made a load test impossible to run, since every virtual user shares the address of
the machine generating the load.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Organization
from apps.documents.models import Document
from apps.signing.models import SigningRequest
from signacore_api.settings import throttle_rate
from utils.throttling import SignacoreRateThrottle

from .pdf_builders import build_flat_pdf
from .test_pdf_form_import import TEST_MEDIA_ROOT

OFFICE_ADDRESS = "203.0.113.10"


class StubView:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def key_for(scope: str, *, address: str, token: str | None = None) -> str:
    throttle = SignacoreRateThrottle()
    throttle.scope = scope
    request = RequestFactory().get("/", HTTP_X_REAL_IP=address)
    view = StubView(token=token) if token else StubView()
    return throttle.get_cache_key(request, view)


class SignerThrottleIdentityTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()

    def test_two_signers_behind_one_address_do_not_share_an_allowance(self) -> None:
        """The office case: one NAT, two people, two documents."""
        first = key_for("signer_preview", address="203.0.113.10", token="11111111-1111-1111-1111-111111111111")
        second = key_for("signer_preview", address="203.0.113.10", token="22222222-2222-2222-2222-222222222222")

        self.assertNotEqual(first, second)

    def test_one_signer_keeps_one_allowance_across_requests(self) -> None:
        repeated = {
            key_for("signer_preview", address="203.0.113.10", token="11111111-1111-1111-1111-111111111111")
            for _ in range(3)
        }

        self.assertEqual(len(repeated), 1)

    def test_asking_for_a_code_is_still_limited_by_address_alone(self) -> None:
        """These stand in front of an unauthenticated caller, so they must not be divisible.

        Keying them by document would let one caller request a code for every signing request it
        could name, which is how a mailbox gets flooded.
        """
        with_token = key_for("signer_otp_send", address="203.0.113.10", token="11111111-1111-1111-1111-111111111111")
        without = key_for("signer_otp_send", address="203.0.113.10")

        self.assertEqual(with_token, without)

    def test_a_different_address_is_still_a_different_allowance(self) -> None:
        first = key_for("signer_preview", address="203.0.113.10", token="11111111-1111-1111-1111-111111111111")
        second = key_for("signer_preview", address="198.51.100.4", token="11111111-1111-1111-1111-111111111111")

        self.assertNotEqual(first, second)

    def test_an_unscoped_throttle_does_not_limit_anything(self) -> None:
        throttle = SignacoreRateThrottle()
        throttle.scope = None

        self.assertIsNone(throttle.get_cache_key(RequestFactory().get("/"), StubView()))

    def test_the_proxy_address_is_preferred_over_one_the_caller_supplies(self) -> None:
        """X-Forwarded-For is client-controlled; X-Real-IP is set by the trusted proxy."""
        throttle = SignacoreRateThrottle()
        request = RequestFactory().get(
            "/",
            HTTP_X_REAL_IP="203.0.113.10",
            HTTP_X_FORWARDED_FOR="198.51.100.4, 203.0.113.10",
        )

        self.assertEqual(throttle.address_of(request), "203.0.113.10")


class ThrottleRateConfigurationTests(SimpleTestCase):
    def test_a_rate_can_be_set_from_the_environment(self) -> None:
        with override_settings():
            import os

            os.environ["SIGNACORE_THROTTLE_SIGNER_PREVIEW"] = "900/minute"
            try:
                self.assertEqual(throttle_rate("signer_preview", "120/minute"), "900/minute")
            finally:
                del os.environ["SIGNACORE_THROTTLE_SIGNER_PREVIEW"]

    def test_the_default_applies_when_nothing_is_set(self) -> None:
        self.assertEqual(throttle_rate("signer_preview", "120/minute"), "120/minute")

    def test_a_long_document_fits_inside_the_preview_allowance(self) -> None:
        """A 31-page packet is 62 requests to read once: a page and a thumbnail each.

        The old limit of 60 a minute was less than a single pass through the longest document the
        product is actually used for.
        """
        allowance = int(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["signer_preview"].split("/")[0])

        self.assertGreater(allowance, 62)


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class ColleaguesBehindOneAddressTests(TestCase):
    """The same thing again, through the real stack rather than the key function.

    The rate is lowered by patching the throttle class rather than the setting: DRF binds
    ``THROTTLE_RATES`` as a class attribute when the module is imported, so overriding
    ``REST_FRAMEWORK`` afterwards does not reach it. The same is true in production, where
    changing ``SIGNACORE_THROTTLE_*`` needs a restart to take effect.
    """

    def setUp(self) -> None:
        cache.clear()
        rates = patch.dict(SignacoreRateThrottle.THROTTLE_RATES, {"signer_preview": "2/minute"})
        rates.start()
        self.addCleanup(rates.stop)
        user = get_user_model().objects.create_user(username="a", email="a@e.com", password="pw123456")
        organization = Organization.objects.create(name="Se7en", created_by=user)
        self.requests = []
        for name in ("Ada", "Grace"):
            document = Document.objects.create(
                title=f"Employment pack for {name}",
                original_pdf=SimpleUploadedFile("f.pdf", build_flat_pdf(), content_type="application/pdf"),
                created_by=user,
                organization=organization,
                status=Document.StatusEnum.SENT,
            )
            self.requests.append(
                SigningRequest.objects.create(
                    document=document,
                    signer_email=f"{name.lower()}@se7en.example",
                    signer_name=name,
                    expires_at=timezone.now() + timedelta(days=7),
                )
            )

    def verified_client(self, signing_request) -> APIClient:
        client = APIClient()
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            client.post(f"/api/sign/{signing_request.id}/otp/send/", HTTP_X_REAL_IP=OFFICE_ADDRESS)
            client.post(
                f"/api/sign/{signing_request.id}/otp/verify/",
                {"otp": "123456"},
                format="json",
                HTTP_X_REAL_IP=OFFICE_ADDRESS,
            )
        return client

    def preview(self, client, signing_request):
        return client.get(f"/api/sign/{signing_request.id}/pages/1/preview/", HTTP_X_REAL_IP=OFFICE_ADDRESS)

    def test_one_colleague_cannot_spend_another_s_allowance(self) -> None:
        ada, grace = self.requests
        ada_client = self.verified_client(ada)
        grace_client = self.verified_client(grace)

        for _ in range(2):
            self.assertEqual(self.preview(ada_client, ada).status_code, 200)
        self.assertEqual(self.preview(ada_client, ada).status_code, 429, "Ada should have spent her own allowance")

        self.assertEqual(
            self.preview(grace_client, grace).status_code,
            200,
            "Grace is at the same desk, on the same address, reading a different document",
        )
