import uuid

from django.conf import settings
from django.db import models

from apps.documents.models import Document, DocumentField
from utils.encryption import EncryptedEmailField, EncryptedTextField
from utils.file_storage import encrypted_file_storage, signature_image_upload_to
from utils.identity import email_digest


class SigningRequest(models.Model):
    class StatusEnum(models.TextChoices):
        PENDING = "PENDING", "Pending"
        OTP_VERIFIED = "OTP_VERIFIED", "OTP Verified"
        SIGNED = "SIGNED", "Signed"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="signing_requests")
    signer_email = EncryptedEmailField()
    signer_email_hash = models.CharField(max_length=64, db_index=True, editable=False, default="")
    signer_name = EncryptedTextField(null=True, blank=True)
    signer_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="signacore_signing_requests",
    )
    status = models.CharField(max_length=32, choices=StatusEnum.choices, default=StatusEnum.PENDING)
    otp_hash = models.CharField(max_length=128, null=True, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    otp_last_sent_at = models.DateTimeField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    ip_address = EncryptedTextField(null=True, blank=True)
    user_agent = EncryptedTextField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.signer_email_hash = email_digest(self.signer_email)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "signer_email" in update_fields:
            kwargs["update_fields"] = set(update_fields) | {"signer_email_hash"}
        return super().save(*args, **kwargs)


class FieldSubmission(models.Model):
    class ValueTypeEnum(models.TextChoices):
        TEXT = "TEXT", "Text"
        SIGNATURE_PNG = "SIGNATURE_PNG", "Signature PNG"
        INITIALS_PNG = "INITIALS_PNG", "Initials PNG"
        CHECKBOX = "CHECKBOX", "Checkbox"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    signing_request = models.ForeignKey(SigningRequest, on_delete=models.CASCADE, related_name="submissions")
    document_field = models.ForeignKey(DocumentField, on_delete=models.CASCADE, related_name="submissions")
    value_type = models.CharField(max_length=32, choices=ValueTypeEnum.choices)
    text_value = EncryptedTextField(null=True, blank=True)
    image_value = models.FileField(
        storage=encrypted_file_storage,
        upload_to=signature_image_upload_to,
        null=True,
        blank=True,
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
