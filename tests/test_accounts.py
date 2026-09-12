from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import AccountProfile, Organization, OrganizationMembership, SocialIdentity
from apps.documents.models import Document
from apps.signing.models import SigningRequest
from services.oauth_client import VerifiedOAuthIdentity


@override_settings(SIGNACORE_SHARED_SECRET="test-signacore-secret")
class OAuthAccountTests(TestCase):
    def setUp(self) -> None:
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
