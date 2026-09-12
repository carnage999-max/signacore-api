from django.test import SimpleTestCase

from apps.accounts.models import AccountProfile, Organization, OrganizationMembership, SocialIdentity
from apps.billing.models import OrganizationSubscription, StripeWebhookEvent
from apps.documents.models import Document, DocumentField
from apps.signing.models import FieldSubmission, SigningRequest


class SignacoreEnumTests(SimpleTestCase):
    def test_social_identity_provider_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in SocialIdentity.ProviderEnum.choices],
            ["GOOGLE", "APPLE"],
        )

    def test_subscription_plan_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in OrganizationSubscription.PlanEnum.choices],
            ["FREE", "PROFESSIONAL", "BUSINESS"],
        )

    def test_stripe_event_status_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in StripeWebhookEvent.ProcessingStatusEnum.choices],
            ["RECEIVED", "PROCESSED", "IGNORED", "FAILED"],
        )

    def test_account_type_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in AccountProfile.AccountTypeEnum.choices],
            ["PLATFORM", "COMPANY", "SIGNER"],
        )

    def test_organization_status_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in Organization.StatusEnum.choices],
            ["ACTIVE", "SUSPENDED", "CLOSED"],
        )

    def test_membership_role_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in OrganizationMembership.RoleEnum.choices],
            ["OWNER", "ADMIN", "MEMBER"],
        )

    def test_document_status_enum_matches_spec(self) -> None:
        self.assertEqual(
            [value for value, _ in Document.StatusEnum.choices],
            ["DRAFT", "SENT", "PARTIALLY_SIGNED", "COMPLETED", "VOIDED"],
        )

    def test_document_field_type_enum_matches_spec(self) -> None:
        self.assertEqual(
            [value for value, _ in DocumentField.FieldTypeEnum.choices],
            ["SIGNATURE", "INITIALS", "TEXT", "CHECKBOX"],
        )

    def test_signing_request_status_enum_matches_spec(self) -> None:
        self.assertEqual(
            [value for value, _ in SigningRequest.StatusEnum.choices],
            ["PENDING", "OTP_VERIFIED", "SIGNED", "EXPIRED"],
        )

    def test_field_submission_value_type_enum_matches_spec(self) -> None:
        self.assertEqual(
            [value for value, _ in FieldSubmission.ValueTypeEnum.choices],
            ["TEXT", "SIGNATURE_PNG", "INITIALS_PNG", "CHECKBOX"],
        )
