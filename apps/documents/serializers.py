from rest_framework import serializers

from apps.signing.serializers import SigningRequestSerializer, SignerInputSerializer

from .models import Document, DocumentField


class DocumentUploadSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    pdf_file = serializers.FileField()

    def validate_pdf_file(self, value):
        content_type = getattr(value, "content_type", "")
        file_name = getattr(value, "name", "")
        if content_type != "application/pdf" and not file_name.lower().endswith(".pdf"):
            raise serializers.ValidationError("Only PDF uploads are allowed.")
        return value


class DocumentFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentField
        fields = (
            "id",
            "field_type",
            "label",
            "page",
            "x",
            "y",
            "width",
            "height",
            "is_required",
            "detection_source",
            "order",
        )
        read_only_fields = ("id", "detection_source")


class ManualDocumentFieldCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentField
        fields = (
            "field_type",
            "label",
            "page",
            "x",
            "y",
            "width",
            "height",
            "is_required",
            "order",
        )


class DocumentFieldUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentField
        fields = (
            "field_type",
            "label",
            "page",
            "x",
            "y",
            "width",
            "height",
            "is_required",
            "order",
        )
        extra_kwargs = {
            "field_type": {"required": False},
            "label": {"required": False},
            "page": {"required": False},
            "x": {"required": False},
            "y": {"required": False},
            "width": {"required": False},
            "height": {"required": False},
            "is_required": {"required": False},
            "order": {"required": False},
        }


class DocumentSerializer(serializers.ModelSerializer):
    fields = DocumentFieldSerializer(many=True, read_only=True)

    class Meta:
        model = Document
        fields = (
            "id",
            "title",
            "status",
            "original_pdf",
            "signed_pdf",
            "created_by",
            "created_at",
            "updated_at",
            "voided_at",
            "voided_reason",
            "fields",
        )
        read_only_fields = ("status", "signed_pdf", "created_at", "updated_at", "voided_at")


class AdminDocumentListSerializer(serializers.ModelSerializer):
    signer_count = serializers.IntegerField(read_only=True)
    signed_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Document
        fields = (
            "id",
            "title",
            "status",
            "created_at",
            "updated_at",
            "signer_count",
            "signed_count",
        )


class AdminDocumentDetailSerializer(DocumentSerializer):
    signer_count = serializers.SerializerMethodField()
    signed_count = serializers.SerializerMethodField()
    signing_requests = SigningRequestSerializer(many=True, read_only=True)

    class Meta(DocumentSerializer.Meta):
        fields = DocumentSerializer.Meta.fields + ("signer_count", "signed_count", "signing_requests")

    def get_signer_count(self, obj: Document) -> int:
        return obj.signing_requests.count()

    def get_signed_count(self, obj: Document) -> int:
        return obj.signing_requests.filter(status="SIGNED").count()


class DocumentSendSerializer(serializers.Serializer):
    signers = SignerInputSerializer(many=True)

    def validate_signers(self, value):
        if not value:
            raise serializers.ValidationError("At least one signer is required.")
        return value


class DocumentUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ("title", "voided_reason")
        extra_kwargs = {
            "title": {"required": False},
            "voided_reason": {"required": False},
        }
