from rest_framework import serializers

from .models import FieldSubmission, SigningRequest


class SigningRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = SigningRequest
        fields = (
            "id",
            "document",
            "signer_email",
            "signer_name",
            "status",
            "otp_expires_at",
            "signed_at",
            "expires_at",
        )


class SignerInputSerializer(serializers.Serializer):
    signer_email = serializers.EmailField()
    signer_name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)


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
