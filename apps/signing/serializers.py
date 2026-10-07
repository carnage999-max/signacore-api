from rest_framework import serializers

from .models import FieldSubmission, SigningRequest


class SigningRequestSerializer(serializers.ModelSerializer):
    # Whether this signer's own copy exists yet. Without it the workspace could not tell which
    # rows have something to download, so it fell back to the whole document being finished - and
    # a signer who had signed could not have their copy fetched until everybody else had too.
    signed_copy_ready = serializers.SerializerMethodField()

    class Meta:
        model = SigningRequest
        fields = (
            "id",
            "document",
            "signer_email",
            "signer_name",
            "status",
            "otp_expires_at",
            "otp_verified_at",
            "signed_at",
            "signed_copy_ready",
            "expires_at",
            "ip_address",
            "user_agent",
        )

    def get_signed_copy_ready(self, obj: SigningRequest) -> bool:
        return bool(obj.signed_pdf)


class SignerInputSerializer(serializers.Serializer):
    signer_email = serializers.EmailField()
    signer_name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)


class SignerOtpSerializer(serializers.Serializer):
    otp = serializers.CharField(max_length=6)


class SignerSessionSerializer(serializers.Serializer):
    session_token = serializers.CharField()


class FieldSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FieldSubmission
        fields = (
            "id",
            "signing_request",
            "document_field",
            "value_type",
            "text_value",
            "image_value",
            "submitted_at",
        )
