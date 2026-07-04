import uuid

from django.conf import settings
from django.db import models


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

