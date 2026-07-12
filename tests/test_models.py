from django.test import SimpleTestCase

from apps.documents.models import Document, DocumentField
from apps.signing.models import FieldSubmission, SigningRequest


class SignacoreEnumTests(SimpleTestCase):
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
