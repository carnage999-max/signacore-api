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
    # Accepted and ignored. An account is for an organisation sending documents; signing one
    # needs no account at all, and never did - the code emailed to the address is the whole of
    # the identity the signing flow consults.
    account_type = serializers.CharField(required=False, allow_blank=True)
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    display_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    invitation_token = serializers.CharField(max_length=255, required=False, allow_blank=True)


class EmailRegistrationSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(min_length=10, max_length=128, trim_whitespace=False, write_only=True)
    display_name = serializers.CharField(max_length=255)
    # Accepted and ignored. An account is for an organisation sending documents; signing one
    # needs no account at all, and never did - the code emailed to the address is the whole of
    # the identity the signing flow consults.
    account_type = serializers.CharField(required=False, allow_blank=True)
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    invitation_token = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def validate(self, attrs):
        # Every account owns an organisation now, so every registration that is not joining one by
        # invitation has to name the organisation it is about to create. This used to be asked only
        # of registrations that said they were for a company, which left two ways to create an
        # organisation with no name, and - because the role was optional - a registration that
        # simply left it out raised instead of answering.
        if not attrs.get("invitation_token", "").strip() and not attrs.get("company_name", "").strip():
            raise serializers.ValidationError({"company_name": ["Company name is required."]})
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


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=64)
    token = serializers.CharField(max_length=128)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_password(self, value: str) -> str:
        # The same rules a password gets anywhere else. A reset is not a way round them.
        validate_password(value)
        return value


class EmailVerificationResendSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class EmailLoginSerializer(serializers.Serializer):
    """What is needed to sign in: who you are and what you know.

    ``account_type`` is still accepted because the sign-in page sends it, and is deliberately
    ignored. Signing in used to require naming the kind of account you were signing in to, and
    getting it wrong was refused as though the password were wrong. The page can only offer
    company or signer, so a platform account could not sign in from anywhere at all. What kind of
    account somebody has is something to be told, not something to be guessed before being let in.
    """

    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    account_type = serializers.CharField(required=False, allow_blank=True)

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
