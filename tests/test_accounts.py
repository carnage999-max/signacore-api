from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import (
    AccountProfile,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    SocialIdentity,
)
from apps.billing.models import OrganizationSubscription
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

    def test_company_owner_can_invite_workspace_member(self) -> None:
        owner = get_user_model().objects.create_user(username="owner", is_active=True, is_staff=True)
        AccountProfile.objects.create(
            user=owner,
            account_type=AccountProfile.AccountTypeEnum.COMPANY,
            email="owner@example.com",
            display_name="Owner",
        )
        organization = Organization.objects.create(name="Example Legal", created_by=owner)
        OrganizationMembership.objects.create(
            organization=organization,
            user=owner,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        OrganizationSubscription.objects.create(
            organization=organization,
            plan=OrganizationSubscription.PlanEnum.BUSINESS,
            status=OrganizationSubscription.StatusEnum.ACTIVE,
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(owner.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(organization.id),
        )

        response = self.client.post(
            "/api/auth/organization/members/",
            {"email": "member@example.com", "role": "MEMBER"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.json())
        invitation = OrganizationInvitation.objects.get(organization=organization)
        self.assertEqual(invitation.email, "member@example.com")
        self.assertEqual(response.json()["item"]["status"], "INVITED")
        self.assertIn("Join workspace", mail.outbox[-1].alternatives[0][0])

    def test_free_owner_cannot_invite_workspace_member(self) -> None:
        owner = get_user_model().objects.create_user(username="free-owner", is_active=True, is_staff=True)
        organization = Organization.objects.create(name="Free Company", created_by=owner)
        OrganizationMembership.objects.create(
            organization=organization,
            user=owner,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(owner.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(organization.id),
        )

        response = self.client.post(
            "/api/auth/organization/members/",
            {"email": "member@example.com", "role": "MEMBER"},
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(response.json()["code"], "PLAN_FEATURE_REQUIRED")

    def test_canceled_business_subscription_cannot_manage_workspace(self) -> None:
        owner = get_user_model().objects.create_user(username="canceled-owner", is_active=True, is_staff=True)
        organization = Organization.objects.create(name="Canceled Company", created_by=owner)
        OrganizationMembership.objects.create(
            organization=organization,
            user=owner,
            role=OrganizationMembership.RoleEnum.OWNER,
        )
        OrganizationSubscription.objects.create(
            organization=organization,
            plan=OrganizationSubscription.PlanEnum.BUSINESS,
            status=OrganizationSubscription.StatusEnum.CANCELED,
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET="test-signacore-secret",
            HTTP_X_SIGNACORE_ADMIN_ID=str(owner.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(organization.id),
        )

        response = self.client.post(
            "/api/auth/organization/members/",
            {"email": "member@example.com", "role": "MEMBER"},
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(response.json()["plan"], "FREE")

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
        # The name is sent whatever role is claimed, because the role is ignored and the
        # organisation it creates still has to be called something.
        return self.client.post(
            "/api/auth/email/register/",
            {
                "email": "owner@example.com",
                "password": self.password,
                "display_name": "Avery Owner",
                "account_type": account_type,
                "company_name": "Example Legal",
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

        return {key: values[0] for key, values in parse_qs(urlparse(verification_url).query).items()}

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
        # The role the page sends is accepted and ignored: an account is for an organisation.
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
        self.assertEqual(response.json()["account_type"], "COMPANY")
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


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class SignInWithoutGuessingAccountTypeTests(TestCase):
    """Signing in should not require naming the kind of account first.

    It did, and getting it wrong was answered as though the password were wrong. The sign-in page
    can only offer company or signer, so a platform account - the kind an administrator of
    SignaCore itself has - could not sign in from anywhere at all, and no amount of trying the
    other buttons would help. What kind of account somebody has is something to tell them once
    they are in, not a password they have to know beforehand.
    """

    password = "Correct-horse-battery-staple-93!"

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_X_SIGNACORE_SECRET="test-signacore-secret")

    def make_account(self, *, account_type: str, active: bool = True):
        user = get_user_model().objects.create_user(
            username=f"user_{account_type.lower()}",
            email="",
            password=self.password,
            is_active=active,
        )
        return AccountProfile.objects.create(
            user=user,
            account_type=account_type,
            email="owner@example.com",
            display_name="Avery Owner",
        )

    def sign_in(self, *, account_type: str = "COMPANY", password: str | None = None):
        return self.client.post(
            "/api/auth/email/login/",
            {
                "email": "owner@example.com",
                "password": password or self.password,
                "account_type": account_type,
            },
            format="json",
        )

    def test_a_platform_account_can_sign_in(self) -> None:
        """The lockout. Neither page offers PLATFORM, so neither could let this account in."""
        self.make_account(account_type=AccountProfile.AccountTypeEnum.PLATFORM)

        response = self.sign_in(account_type="COMPANY")

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["account_type"], "PLATFORM")

    def test_the_page_you_came_from_does_not_decide_whether_you_are_let_in(self) -> None:
        self.make_account(account_type=AccountProfile.AccountTypeEnum.SIGNER)

        from_the_wrong_page = self.sign_in(account_type="COMPANY")

        self.assertEqual(from_the_wrong_page.status_code, 200, from_the_wrong_page.json())
        self.assertEqual(from_the_wrong_page.json()["account_type"], "SIGNER")

    def test_an_unverified_account_is_told_so_rather_than_that_its_password_is_wrong(self) -> None:
        """The one person who can fix it was being told the one thing that was not true."""
        self.make_account(account_type=AccountProfile.AccountTypeEnum.COMPANY, active=False)

        response = self.sign_in()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "EMAIL_NOT_VERIFIED")

    def test_a_wrong_password_still_says_only_that(self) -> None:
        """Whether an account exists must not be learnable by trying passwords against it."""
        self.make_account(account_type=AccountProfile.AccountTypeEnum.COMPANY, active=False)

        missing = self.sign_in(password="not-the-password")
        self.client.credentials(HTTP_X_SIGNACORE_SECRET="test-signacore-secret")
        unknown = self.client.post(
            "/api/auth/email/login/",
            {"email": "nobody@example.com", "password": self.password, "account_type": "COMPANY"},
            format="json",
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(missing.json()["detail"], unknown.json()["detail"])

    def test_a_new_verification_link_can_be_asked_for(self) -> None:
        profile = self.make_account(account_type=AccountProfile.AccountTypeEnum.COMPANY, active=False)
        mail.outbox.clear()

        response = self.client.post("/api/auth/email/verify/resend/", {"email": "owner@example.com"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(profile.user.is_active is False)

    def test_asking_for_a_link_does_not_say_who_has_an_account(self) -> None:
        self.make_account(account_type=AccountProfile.AccountTypeEnum.COMPANY, active=False)
        mail.outbox.clear()

        known = self.client.post("/api/auth/email/verify/resend/", {"email": "owner@example.com"}, format="json")
        cache.clear()
        unknown = self.client.post("/api/auth/email/verify/resend/", {"email": "nobody@example.com"}, format="json")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json()["detail"], unknown.json()["detail"])
        self.assertEqual(len(mail.outbox), 1, "only the account that exists is mailed")

    def test_a_verified_account_is_not_sent_another_link(self) -> None:
        self.make_account(account_type=AccountProfile.AccountTypeEnum.COMPANY, active=True)
        mail.outbox.clear()

        self.client.post("/api/auth/email/verify/resend/", {"email": "owner@example.com"}, format="json")

        self.assertEqual(len(mail.outbox), 0)


@override_settings(
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_APP_URL="https://mysignacore.com",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PasswordResetTests(TestCase):
    """A way back in, and for an account made through a provider, a second way in at all.

    There was none. An account created through Google or Apple is given an unusable password, so
    losing the provider account lost the SignaCore account outright, with nothing anybody could do
    from the product.
    """

    password = "Correct-horse-battery-staple-93!"
    replacement = "Rhubarb-cartwheel-lantern-77?"

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_X_SIGNACORE_SECRET="test-signacore-secret")

    def make_account(self, *, with_password: bool = True, active: bool = True):
        user = get_user_model().objects.create_user(
            username="owner", email="", password=self.password if with_password else None, is_active=active
        )
        if not with_password:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        AccountProfile.objects.create(
            user=user,
            account_type=AccountProfile.AccountTypeEnum.COMPANY,
            email="owner@example.com",
            display_name="Avery Owner",
        )
        return user

    def link_values(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlparse

        url = next(
            line.removeprefix("Link: ") for line in mail.outbox[-1].body.splitlines() if line.startswith("Link: ")
        )
        return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}

    def request_reset(self, email: str = "owner@example.com"):
        return self.client.post("/api/auth/password/reset/", {"email": email}, format="json")

    def confirm(self, *, password: str | None = None, **overrides):
        values = {**self.link_values(), "password": password or self.replacement, **overrides}
        return self.client.post("/api/auth/password/reset/confirm/", values, format="json")

    def sign_in(self, password: str):
        self.client.credentials(HTTP_X_SIGNACORE_SECRET="test-signacore-secret")
        return self.client.post(
            "/api/auth/email/login/",
            {"email": "owner@example.com", "password": password, "account_type": "COMPANY"},
            format="json",
        )

    def test_a_password_can_be_replaced(self) -> None:
        self.make_account()
        self.request_reset()

        self.assertEqual(self.confirm().status_code, 200)
        cache.clear()
        self.assertEqual(self.sign_in(self.replacement).status_code, 200)
        cache.clear()
        self.assertEqual(self.sign_in(self.password).status_code, 401, "the old password must stop working")

    def test_an_account_made_through_a_provider_can_be_given_a_password(self) -> None:
        """The case there was no answer to at all."""
        user = self.make_account(with_password=False)
        self.assertFalse(user.has_usable_password())
        self.request_reset()

        self.assertEqual(self.confirm().status_code, 200)
        cache.clear()
        self.assertEqual(self.sign_in(self.replacement).status_code, 200)

    def test_the_email_says_which_of_the_two_is_happening(self) -> None:
        self.make_account(with_password=False)
        self.request_reset()

        self.assertIn("Set a SignaCore password", mail.outbox[-1].subject)
        self.assertIn("Google or Apple", mail.outbox[-1].body)

    def test_a_link_cannot_be_used_twice(self) -> None:
        """A reset link read out of an old mailbox must not change the password again."""
        self.make_account()
        self.request_reset()
        values = self.link_values()

        first = self.client.post(
            "/api/auth/password/reset/confirm/", {**values, "password": self.replacement}, format="json"
        )
        second = self.client.post(
            "/api/auth/password/reset/confirm/", {**values, "password": "Another-one-entirely-41!"}, format="json"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_setting_a_password_verifies_an_account_still_waiting(self) -> None:
        """Opening the link proves the address receives mail, which is what verification is for."""
        user = self.make_account(active=False)
        self.request_reset()

        self.assertEqual(self.confirm().status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_a_weak_password_is_refused(self) -> None:
        self.make_account()
        self.request_reset()

        self.assertEqual(self.confirm(password="password").status_code, 400)

    def test_a_tampered_token_is_refused(self) -> None:
        self.make_account()
        self.request_reset()

        self.assertEqual(self.confirm(token="not-the-token").status_code, 400)

    def test_asking_does_not_say_who_has_an_account(self) -> None:
        self.make_account()
        mail.outbox.clear()

        known = self.request_reset()
        cache.clear()
        unknown = self.request_reset("nobody@example.com")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json()["detail"], unknown.json()["detail"])
        self.assertEqual(len(mail.outbox), 1, "only the address with an account is mailed")
