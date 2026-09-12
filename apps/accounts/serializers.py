from rest_framework import serializers

from .models import AccountProfile, SocialIdentity


class OAuthExchangeSerializer(serializers.Serializer):
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
