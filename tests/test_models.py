import fitz
from django.test import SimpleTestCase

from apps.accounts.models import AccountProfile, OAuthIntentEnum, Organization, OrganizationMembership, SocialIdentity
from apps.billing.models import BillingPlanConfiguration, OrganizationSubscription, StripeWebhookEvent
from apps.documents.models import Document, DocumentField
from apps.signing.models import FieldSubmission, SigningRequest
from services.pdf_engine import PDFEngine


class SignacoreEnumTests(SimpleTestCase):
    def test_oauth_intent_enum_is_explicit(self) -> None:
        self.assertEqual([value for value, _ in OAuthIntentEnum.choices], ["LOGIN", "REGISTER"])

    def test_billing_plan_configuration_enums_are_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in BillingPlanConfiguration.PlanEnum.choices],
            ["FREE", "PROFESSIONAL", "BUSINESS", "ENTERPRISE"],
        )
        self.assertEqual(
            [value for value, _ in BillingPlanConfiguration.BillingIntervalEnum.choices],
            ["month", "year"],
        )

    def test_social_identity_provider_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in SocialIdentity.ProviderEnum.choices],
            ["GOOGLE", "APPLE"],
        )

    def test_subscription_plan_enum_is_explicit(self) -> None:
        self.assertEqual(
            [value for value, _ in OrganizationSubscription.PlanEnum.choices],
            ["FREE", "PROFESSIONAL", "BUSINESS", "ENTERPRISE"],
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

    def test_pdf_text_font_size_tracks_field_rectangle(self) -> None:
        self.assertEqual(PDFEngine._field_text_font_size(fitz.Rect(0, 0, 120, 12)), 8.64)
        self.assertEqual(PDFEngine._field_text_font_size(fitz.Rect(0, 0, 180, 24)), 12.0)
        self.assertEqual(PDFEngine._field_text_font_size(fitz.Rect(0, 0, 20, 8)), 7.0)

    def test_inferred_field_height_uses_pdf_line_bounds(self) -> None:
        engine = PDFEngine()
        words = [
            (72.0, 100.0, 105.0, 111.5, "Tenant", 0, 0, 0),
            (109.0, 100.0, 150.0, 111.5, "Name", 0, 0, 1),
        ]

        self.assertEqual(engine._line_height_for_words(words), 11.5)
        self.assertIsNone(engine._line_height_for_words([]))

    def test_drawn_line_uses_the_closest_document_line_height(self) -> None:
        engine = PDFEngine()
        line_words = [
            [(72.0, 90.0, 130.0, 101.0, "Tenant", 0, 0, 0)],
            [(72.0, 190.0, 130.0, 205.5, "Signature", 0, 1, 0)],
        ]

        self.assertEqual(engine._line_height_near_rect(fitz.Rect(180, 192, 340, 193), line_words), 15.5)
