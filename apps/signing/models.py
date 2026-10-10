import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.documents.models import Document, DocumentField
from utils.encryption import EncryptedEmailField, EncryptedTextField
from utils.file_storage import encrypted_file_storage, signature_image_upload_to, signer_copy_upload_to
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
    # When this signer last passed the emailed code. The status alone cannot say whether somebody
    # is signing now or verified days ago and walked away: OTP_VERIFIED is kept until the link
    # expires, which is a week. A signing session lasts an hour, so this is what makes the
    # difference between "in progress" and "opened it once" expressible.
    otp_verified_at = models.DateTimeField(null=True, blank=True)
    # This signer's own completed copy, carrying their answers and nobody else's.
    #
    # A document used to hold one copy for everybody, flattened from every signer's submissions at
    # once. That is right for a contract several parties put their names to and wrong for anything
    # sent to a group to fill in separately: eight people answering the same survey produced one
    # page with eight sets of answers drawn over each other, which every one of them then received.
    signed_pdf = models.FileField(
        storage=encrypted_file_storage,
        upload_to=signer_copy_upload_to,
        # Well clear of the default hundred, so a longer path can never be refused as suspicious.
        max_length=255,
        null=True,
        blank=True,
    )
    # When the follow-up about SignaCore itself went to this signer, which is the only email we
    # send that is not about the document in front of them. Recorded per request, but checked per
    # address: somebody who signs ten documents hears from us once.
    follow_up_sent_at = models.DateTimeField(null=True, blank=True)
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


class SigningEvent(models.Model):
    """One thing that happened to a signing request, and where it happened from.

    SigningRequest carries a single ip_address and user_agent, written when the code is verified
    and overwritten when the document is signed. That is a snapshot, not a history: a signer who
    verified on one network and signed on another left no trace of the first, and a document
    nobody ever opened looked exactly like one opened ten times.

    Rows here are only ever added. Nothing in the product updates or deletes one, because the
    value of a trail is that it was not edited afterwards.
    """

    class EventEnum(models.TextChoices):
        SENT = "SENT", "Signing request sent"
        OPENED = "OPENED", "Document opened"
        CODE_SENT = "CODE_SENT", "Verification code sent"
        CODE_VERIFIED = "CODE_VERIFIED", "Email verified"
        SIGNED = "SIGNED", "Signed and submitted"
        REOPENED = "REOPENED", "Reopened for correction"
        COPY_DELIVERED = "COPY_DELIVERED", "Completed copy emailed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    signing_request = models.ForeignKey(SigningRequest, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=32, choices=EventEnum.choices)
    # Set explicitly rather than by auto_now_add, so the backfill of existing requests can record
    # when each thing actually happened instead of when the migration ran.
    at = models.DateTimeField(default=timezone.now)
    ip_address = EncryptedTextField(null=True, blank=True)
    user_agent = EncryptedTextField(null=True, blank=True)
    # Anything worth saying about this one event, such as the address a code went to. Encrypted,
    # because for most events that is somebody's email address.
    detail = EncryptedTextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("at", "created_at")
        indexes = [models.Index(fields=["signing_request", "at"])]

    def __str__(self) -> str:
        return f"{self.get_event_display()} at {self.at:%Y-%m-%d %H:%M:%S}"


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
