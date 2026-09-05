import secrets

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.signing.serializers import SigningRequestSerializer, SignerInputSerializer

from .models import AdminAuditLog, Document, DocumentField


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


class AdminUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "is_staff",
            "is_superuser",
            "is_active",
            "last_login",
            "date_joined",
        )
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.get_full_name()


class AdminLoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=255)
    password = serializers.CharField(trim_whitespace=False)

    def validate(self, attrs):
        identifier = attrs["identifier"].strip()
        password = attrs["password"]
        user_model = get_user_model()
        username = identifier

        if "@" in identifier:
            user = user_model.objects.filter(email__iexact=identifier).first()
            if user:
                username = user.get_username()

        user = authenticate(username=username, password=password)
        if user is None or not user.is_active or not user.is_staff:
            raise serializers.ValidationError("Invalid admin credentials.")

        attrs["user"] = user
        return attrs


class AdminUserCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    is_superuser = serializers.BooleanField(default=False)
    password = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)

    def validate_username(self, value: str) -> str:
        user_model = get_user_model()
        username = value.strip()
        if user_model.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError("An admin with this username already exists.")
        return username

    def validate_email(self, value: str) -> str:
        user_model = get_user_model()
        email = value.strip().lower()
        if user_model.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An admin with this email already exists.")
        return email

    def validate(self, attrs):
        password = attrs.get("password") or secrets.token_urlsafe(14)
        validate_password(password)
        attrs["temporary_password"] = password
        return attrs

    def create(self, validated_data):
        user_model = get_user_model()
        temporary_password = validated_data.pop("temporary_password")
        validated_data.pop("password", None)
        is_superuser = validated_data.pop("is_superuser", False)
        user = user_model.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=temporary_password,
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            is_staff=True,
            is_superuser=is_superuser,
            is_active=True,
        )
        user.temporary_password = temporary_password
        return user


class AdminPasswordChangeSerializer(serializers.Serializer):
    password = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)

    def validate(self, attrs):
        password = attrs.get("password") or secrets.token_urlsafe(14)
        validate_password(password)
        attrs["temporary_password"] = password
        return attrs


class AdminAuditLogSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True)

    class Meta:
        model = AdminAuditLog
        fields = (
            "id",
            "actor",
            "actor_username",
            "actor_email",
            "action",
            "target_type",
            "target_id",
            "summary",
            "metadata",
            "ip_address",
            "user_agent",
            "created_at",
        )
        read_only_fields = fields
