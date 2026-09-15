from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import (
    AccountProfile,
    OAuthIntentEnum,
    OrganizationInvitation,
    OrganizationMembership,
    SocialIdentity,
)


class OAuthExchangeSerializer(serializers.Serializer):
    intent = serializers.ChoiceField(choices=OAuthIntentEnum.choices)
    provider = serializers.ChoiceField(choices=SocialIdentity.ProviderEnum.choices)
    code = serializers.CharField(max_length=4096, trim_whitespace=False)
    redirect_uri = serializers.URLField(max_length=2048)
    nonce = serializers.CharField(min_length=16, max_length=255)
    account_type = serializers.ChoiceField(
        choices=(
            AccountProfile.AccountTypeEnum.COMPANY,
            AccountProfile.AccountTypeEnum.SIGNER,
        )
    )
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    display_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    invitation_token = serializers.CharField(max_length=255, required=False, allow_blank=True)


class EmailRegistrationSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(min_length=10, max_length=128, trim_whitespace=False, write_only=True)
    display_name = serializers.CharField(max_length=255)
    account_type = serializers.ChoiceField(
        choices=(
            AccountProfile.AccountTypeEnum.COMPANY,
            AccountProfile.AccountTypeEnum.SIGNER,
        )
    )
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    invitation_token = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def validate(self, attrs):
        if (
            attrs["account_type"] == AccountProfile.AccountTypeEnum.COMPANY
            and not attrs.get("company_name", "").strip()
        ):
            raise serializers.ValidationError(
                {"company_name": ["Company name is required for a company account."]}
            )
        return attrs


class OrganizationMemberSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    user_id = serializers.IntegerField(allow_null=True)
    name = serializers.CharField()
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=OrganizationMembership.RoleEnum.choices)
    status = serializers.ChoiceField(choices=OrganizationMembership.StatusEnum.choices)
    joined_at = serializers.DateTimeField(allow_null=True)


class OrganizationInvitationSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    role = serializers.ChoiceField(
        choices=OrganizationInvitation.RoleEnum.choices,
        default=OrganizationInvitation.RoleEnum.MEMBER,
    )

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class EmailLoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    account_type = serializers.ChoiceField(
        choices=(
            AccountProfile.AccountTypeEnum.COMPANY,
            AccountProfile.AccountTypeEnum.SIGNER,
        )
    )

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class EmailVerificationSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=255)
    token = serializers.CharField(max_length=255)


class AccountSessionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()
    display_name = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    full_name = serializers.CharField()
    account_type = serializers.ChoiceField(choices=AccountProfile.AccountTypeEnum.choices)
    is_staff = serializers.BooleanField()
    is_superuser = serializers.BooleanField()
    is_new = serializers.BooleanField()
    organizations = serializers.ListField(child=serializers.DictField())


class AccountSigningRequestSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    document_title = serializers.CharField()
    company_name = serializers.CharField()
    status = serializers.CharField()
    expires_at = serializers.DateTimeField(allow_null=True)
    signed_at = serializers.DateTimeField(allow_null=True)
