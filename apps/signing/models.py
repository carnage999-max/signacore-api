import uuid

from django.db import models

from apps.documents.models import Document, DocumentField
from utils.encryption import EncryptedEmailField, EncryptedTextField


class SigningRequest(models.Model):
    class StatusEnum(models.TextChoices):
        PENDING = "PENDING", "Pending"
        OTP_VERIFIED = "OTP_VERIFIED", "OTP Verified"
        SIGNED = "SIGNED", "Signed"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="signing_requests")
    signer_email = EncryptedEmailField()
    signer_name = EncryptedTextField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=StatusEnum.choices, default=StatusEnum.PENDING)
    otp_hash = models.CharField(max_length=128, null=True, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    ip_address = EncryptedTextField(null=True, blank=True)
    user_agent = EncryptedTextField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class FieldSubmission(models.Model):
    class ValueTypeEnum(models.TextChoices):
        TEXT = "TEXT", "Text"
        SIGNATURE_PNG = "SIGNATURE_PNG", "Signature PNG"
        INITIALS_PNG = "INITIALS_PNG", "Initials PNG"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    signing_request = models.ForeignKey(SigningRequest, on_delete=models.CASCADE, related_name="submissions")
    document_field = models.ForeignKey(DocumentField, on_delete=models.CASCADE, related_name="submissions")
    value_type = models.CharField(max_length=32, choices=ValueTypeEnum.choices)
    text_value = EncryptedTextField(null=True, blank=True)
    image_value = models.FileField(upload_to="signacore/sigs/", null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

