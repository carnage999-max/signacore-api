from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core import mail
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import AccountProfile, Organization, OrganizationMembership, SocialIdentity
from apps.documents.models import Document
from apps.signing.models import SigningRequest
from services.oauth_client import VerifiedOAuthIdentity


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class OAuthAccountTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_X_SIGNACORE_SECRET="test-signacore-secret")

    @patch("apps.accounts.views.exchange_oauth_code")
    def test_google_company_signup_creates_owner_workspace(self, exchange_code) -> None:
        exchange_code.return_value = VerifiedOAuthIdentity(
            provider="GOOGLE",
            subject="google-user-123",
            email="owner@example.com",
            display_name="Avery Owner",
        )

        response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "REGISTER",
                "provider": "GOOGLE",
                "code": "authorization-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/google",
                "nonce": "a-secure-login-nonce",
                "account_type": "COMPANY",
                "company_name": "Example Legal",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertTrue(payload["is_new"])
        self.assertEqual(payload["account_type"], "COMPANY")
        self.assertTrue(payload["is_staff"])
        self.assertEqual(payload["email"], "owner@example.com")
        organization = Organization.objects.get(name="Example Legal")
        membership = OrganizationMembership.objects.get(organization=organization)
        self.assertEqual(membership.role, OrganizationMembership.RoleEnum.OWNER)
        identity = SocialIdentity.objects.get(provider=SocialIdentity.ProviderEnum.GOOGLE)
        self.assertEqual(identity.subject, "google-user-123")
        self.assertEqual(identity.email, "owner@example.com")
        self.assertEqual(identity.user.email, "")
        self.assertEqual(mail.outbox[-1].subject, "Welcome to SignaCore")

    @patch("apps.accounts.views.exchange_oauth_code")
    def test_existing_identity_cannot_change_account_role(self, exchange_code) -> None:
        exchange_code.return_value = VerifiedOAuthIdentity(
            provider="GOOGLE",
            subject="google-user-456",
            email="signer@example.com",
            display_name="Sam Signer",
        )
        document_owner = get_user_model().objects.create_user(username="document-owner")
        organization = Organization.objects.create(name="Sender Company", created_by=document_owner)
        document = Document.objects.create(
            title="Offer to sign",
            original_pdf=ContentFile(b"%PDF-1.4", name="offer.pdf"),
            created_by=document_owner,
            organization=organization,
        )
        signing_request = SigningRequest.objects.create(
            document=document,
            signer_email="signer@example.com",
        )
        first_response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "REGISTER",
                "provider": "GOOGLE",
                "code": "first-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/google",
                "nonce": "first-secure-login-nonce",
                "account_type": "SIGNER",
            },
            format="json",
        )
        second_response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "LOGIN",
                "provider": "GOOGLE",
                "code": "second-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/google",
                "nonce": "second-secure-login-nonce",
                "account_type": "SIGNER",
            },
            format="json",
        )

        self.assertEqual(first_response.status_code, 200, first_response.json())
        self.assertEqual(second_response.status_code, 200, second_response.json())
        self.assertFalse(second_response.json()["is_new"])
        self.assertEqual(second_response.json()["account_type"], AccountProfile.AccountTypeEnum.SIGNER)
        self.assertEqual(SocialIdentity.objects.count(), 1)
        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(mail.outbox[-1].subject, "New sign-in to your SignaCore account")
        signing_request.refresh_from_db()
        self.assertEqual(signing_request.signer_user_id, second_response.json()["id"])

        history_response = self.client.get(
            "/api/auth/account/signing-requests/",
            HTTP_X_SIGNACORE_ACCOUNT_ID=str(second_response.json()["id"]),
        )
        self.assertEqual(history_response.status_code, 200, history_response.json())
        self.assertEqual(history_response.json()["items"][0]["document_title"], "Offer to sign")

    @patch("apps.accounts.views.exchange_oauth_code")
    def test_company_signup_requires_company_name(self, exchange_code) -> None:
        exchange_code.return_value = VerifiedOAuthIdentity(
            provider="APPLE",
            subject="apple-user-789",
            email="owner@privaterelay.appleid.com",
            display_name="",
        )
        response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "REGISTER",
                "provider": "APPLE",
                "code": "authorization-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/apple",
                "nonce": "a-secure-login-nonce",
                "account_type": "COMPANY",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.json())
        self.assertIn("company_name", response.json())

    @patch("apps.accounts.views.exchange_oauth_code")
    def test_login_does_not_create_an_unknown_account(self, exchange_code) -> None:
        exchange_code.return_value = VerifiedOAuthIdentity(
            provider="GOOGLE",
            subject="unknown-google-user",
            email="unknown@example.com",
            display_name="Unknown User",
        )

        response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "LOGIN",
                "provider": "GOOGLE",
                "code": "authorization-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/google",
                "nonce": "a-secure-login-nonce",
                "account_type": "COMPANY",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 404, response.json())
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertEqual(SocialIdentity.objects.count(), 0)

    @patch("apps.accounts.views.exchange_oauth_code")
    def test_registration_links_provider_to_existing_verified_email_account(self, exchange_code) -> None:
        user = get_user_model().objects.create_user(
            username="email_account",
            password="Correct-horse-battery-staple-93!",
            is_active=True,
            is_staff=True,
        )
        AccountProfile.objects.create(
            user=user,
            account_type=AccountProfile.AccountTypeEnum.COMPANY,
            email="owner@example.com",
            display_name="Avery Owner",
        )
        organization = Organization.objects.create(name="Example Legal", created_by=user)
        OrganizationMembership.objects.create(
            organization=organization,
            user=user,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        exchange_code.return_value = VerifiedOAuthIdentity(
            provider="GOOGLE",
            subject="google-user-for-email-account",
            email="owner@example.com",
            display_name="Avery Owner",
        )

        response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "REGISTER",
                "provider": "GOOGLE",
                "code": "authorization-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/google",
                "nonce": "a-secure-login-nonce",
                "account_type": "COMPANY",
                "company_name": "Example Legal",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertFalse(response.json()["is_new"])
        self.assertEqual(response.json()["id"], user.id)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(SocialIdentity.objects.get().user, user)

    @patch("apps.accounts.views.exchange_oauth_code")
    def test_login_links_provider_to_existing_verified_email_account(self, exchange_code) -> None:
        user = get_user_model().objects.create_user(
            username="email_account",
            password="Correct-horse-battery-staple-93!",
            is_active=True,
            is_staff=True,
        )
        AccountProfile.objects.create(
            user=user,
            account_type=AccountProfile.AccountTypeEnum.COMPANY,
            email="owner@example.com",
            display_name="Avery Owner",
        )
        organization = Organization.objects.create(name="Example Legal", created_by=user)
        OrganizationMembership.objects.create(
            organization=organization,
            user=user,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        exchange_code.return_value = VerifiedOAuthIdentity(
            provider="GOOGLE",
            subject="google-user-for-email-login",
            email="owner@example.com",
            display_name="Avery Owner",
        )

        response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "LOGIN",
                "provider": "GOOGLE",
                "code": "authorization-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/google",
                "nonce": "a-secure-login-nonce",
                "account_type": "COMPANY",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertFalse(response.json()["is_new"])
        self.assertEqual(response.json()["id"], user.id)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(SocialIdentity.objects.get().user, user)

    @patch("apps.accounts.views.exchange_oauth_code")
    def test_oauth_activates_matching_unverified_email_account(self, exchange_code) -> None:
        user = get_user_model().objects.create_user(
            username="email_account",
            password="Correct-horse-battery-staple-93!",
            is_active=False,
            is_staff=True,
        )
        AccountProfile.objects.create(
            user=user,
            account_type=AccountProfile.AccountTypeEnum.COMPANY,
            email="owner@example.com",
            display_name="Avery Owner",
        )
        organization = Organization.objects.create(name="Example Legal", created_by=user)
        OrganizationMembership.objects.create(
            organization=organization,
            user=user,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        exchange_code.return_value = VerifiedOAuthIdentity(
            provider="GOOGLE",
            subject="google-user-for-unverified-email",
            email="owner@example.com",
            display_name="Avery Owner",
        )

        response = self.client.post(
            "/api/auth/oauth/exchange/",
            {
                "intent": "LOGIN",
                "provider": "GOOGLE",
                "code": "authorization-code",
                "redirect_uri": "https://mysignacore.com/api/auth/oauth/callback/google",
                "nonce": "a-secure-login-nonce",
                "account_type": "COMPANY",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(response.json()["is_new"])
        self.assertEqual(SocialIdentity.objects.get().user, user)


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class EmailAccountTests(TestCase):
    password = "Correct-horse-battery-staple-93!"

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_X_SIGNACORE_SECRET="test-signacore-secret")

    def register(self, *, account_type: str = "COMPANY"):
        return self.client.post(
            "/api/auth/email/register/",
            {
                "email": "owner@example.com",
                "password": self.password,
                "display_name": "Avery Owner",
                "account_type": account_type,
                "company_name": "Example Legal" if account_type == "COMPANY" else "",
            },
            format="json",
        )

    def verification_values(self) -> dict[str, str]:
        verification_url = next(
            line.removeprefix("Verification link: ")
            for line in mail.outbox[-1].body.splitlines()
            if line.startswith("Verification link: ")
        )
        from urllib.parse import parse_qs, urlparse

        return {
            key: values[0]
            for key, values in parse_qs(urlparse(verification_url).query).items()
        }

    def test_company_registration_requires_email_verification(self) -> None:
        response = self.register()

        self.assertEqual(response.status_code, 201, response.json())
        user = get_user_model().objects.get()
        self.assertFalse(user.is_active)
        self.assertEqual(user.email, "")
        self.assertTrue(user.check_password(self.password))
        self.assertEqual(user.signacore_profile.email, "owner@example.com")
        self.assertEqual(user.signacore_profile.display_name, "Avery Owner")
        self.assertEqual(Organization.objects.get().name, "Example Legal")
        self.assertEqual(mail.outbox[0].subject, "Verify your SignaCore account")
        self.assertIn("https://mysignacore.com/api/auth/email/verify", mail.outbox[0].body)

        verification_response = self.client.post(
            "/api/auth/email/verify/",
            self.verification_values(),
            format="json",
        )

        self.assertEqual(verification_response.status_code, 200, verification_response.json())
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(verification_response.json()["account_type"], "COMPANY")
        self.assertTrue(verification_response.json()["is_staff"])
        self.assertEqual(mail.outbox[-1].subject, "Welcome to SignaCore")

    def test_email_verification_link_survives_repeat_requests(self) -> None:
        self.register()
        values = self.verification_values()

        first_response = self.client.post(
            "/api/auth/email/verify/",
            values,
            format="json",
        )
        second_response = self.client.post(
            "/api/auth/email/verify/",
            values,
            format="json",
        )

        self.assertEqual(first_response.status_code, 200, first_response.json())
        self.assertEqual(second_response.status_code, 200, second_response.json())
        self.assertEqual(second_response.json()["account_type"], "COMPANY")
        self.assertEqual(
            [message.subject for message in mail.outbox].count("Welcome to SignaCore"),
            1,
        )

    def test_verified_account_can_log_in_with_email_and_password(self) -> None:
        self.register(account_type="SIGNER")
        self.client.post("/api/auth/email/verify/", self.verification_values(), format="json")

        response = self.client.post(
            "/api/auth/email/login/",
            {
                "email": "OWNER@EXAMPLE.COM",
                "password": self.password,
                "account_type": "SIGNER",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["email"], "owner@example.com")
        self.assertEqual(response.json()["account_type"], "SIGNER")
        self.assertEqual(mail.outbox[-1].subject, "New sign-in to your SignaCore account")

    def test_login_uses_a_generic_error_for_invalid_credentials(self) -> None:
        response = self.client.post(
            "/api/auth/email/login/",
            {
                "email": "missing@example.com",
                "password": self.password,
                "account_type": "COMPANY",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 401, response.json())
        self.assertEqual(response.json()["detail"], "The email or password is incorrect.")

    def test_unverified_registration_can_resend_its_verification_email(self) -> None:
        first_response = self.register()
        second_response = self.register()

        self.assertEqual(first_response.status_code, 201, first_response.json())
        self.assertEqual(second_response.status_code, 200, second_response.json())
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(len(mail.outbox), 2)
