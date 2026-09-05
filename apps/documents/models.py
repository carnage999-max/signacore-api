import uuid

from django.conf import settings
from django.db import models

from utils.encryption import EncryptedEmailField, EncryptedTextField


class Document(models.Model):
    class StatusEnum(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"
        PARTIALLY_SIGNED = "PARTIALLY_SIGNED", "Partially Signed"
        COMPLETED = "COMPLETED", "Completed"
        VOIDED = "VOIDED", "Voided"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    original_pdf = models.FileField(upload_to="signacore/originals/")
    signed_pdf = models.FileField(upload_to="signacore/signed/", null=True, blank=True)
    status = models.CharField(max_length=32, choices=StatusEnum.choices, default=StatusEnum.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="signacore_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_reason = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.title


class DocumentField(models.Model):
    class FieldTypeEnum(models.TextChoices):
        SIGNATURE = "SIGNATURE", "Signature"
        INITIALS = "INITIALS", "Initials"
        TEXT = "TEXT", "Text"
        CHECKBOX = "CHECKBOX", "Checkbox"

    class DetectionSourceEnum(models.TextChoices):
        ACROFORM = "ACROFORM", "AcroForm"
        HEURISTIC = "HEURISTIC", "Heuristic"
        MANUAL = "MANUAL", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="fields")
    field_type = models.CharField(max_length=32, choices=FieldTypeEnum.choices)
    label = models.CharField(max_length=255)
    page = models.PositiveIntegerField()
    x = models.FloatField()
    y = models.FloatField()
    width = models.FloatField()
    height = models.FloatField()
    is_required = models.BooleanField(default=True)
    detection_source = models.CharField(max_length=32, choices=DetectionSourceEnum.choices)
    order = models.PositiveIntegerField()

    class Meta:
        ordering = ("page", "order")

    def __str__(self) -> str:
        return f"{self.document.title}: {self.label}"


class AdminAuditLog(models.Model):
    class ActionEnum(models.TextChoices):
        LOGIN = "LOGIN", "Login"
        LOGOUT = "LOGOUT", "Logout"
        DOCUMENT_LIST = "DOCUMENT_LIST", "Document List"
        DOCUMENT_VIEW = "DOCUMENT_VIEW", "Document View"
        DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD", "Document Upload"
        DOCUMENT_UPDATE = "DOCUMENT_UPDATE", "Document Update"
        DOCUMENT_VOID = "DOCUMENT_VOID", "Document Void"
        DOCUMENT_DOWNLOAD = "DOCUMENT_DOWNLOAD", "Document Download"
        FIELD_CREATE = "FIELD_CREATE", "Field Create"
        FIELD_UPDATE = "FIELD_UPDATE", "Field Update"
        FIELD_DELETE = "FIELD_DELETE", "Field Delete"
        SIGNING_REQUEST_SEND = "SIGNING_REQUEST_SEND", "Signing Request Send"
        SIGNING_REQUEST_RESEND = "SIGNING_REQUEST_RESEND", "Signing Request Resend"
        ADMIN_USER_LIST = "ADMIN_USER_LIST", "Admin User List"
        ADMIN_USER_CREATE = "ADMIN_USER_CREATE", "Admin User Create"
        ADMIN_PASSWORD_CHANGE = "ADMIN_PASSWORD_CHANGE", "Admin Password Change"
        AUDIT_LOG_LIST = "AUDIT_LOG_LIST", "Audit Log List"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="signacore_audit_logs",
    )
    actor_email = EncryptedEmailField(null=True, blank=True)
    action = models.CharField(max_length=64, choices=ActionEnum.choices)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=128, blank=True)
    summary = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = EncryptedTextField(null=True, blank=True)
    user_agent = EncryptedTextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.action}: {self.summary}"
