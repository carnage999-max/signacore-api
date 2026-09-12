import uuid

from django.db import models

from apps.accounts.models import Organization


class OrganizationSubscription(models.Model):
    class PlanEnum(models.TextChoices):
        FREE = "FREE", "Free"
        PROFESSIONAL = "PROFESSIONAL", "Professional"
        BUSINESS = "BUSINESS", "Business"

    class StatusEnum(models.TextChoices):
        NONE = "NONE", "None"
        INCOMPLETE = "INCOMPLETE", "Incomplete"
        INCOMPLETE_EXPIRED = "INCOMPLETE_EXPIRED", "Incomplete Expired"
        TRIALING = "TRIALING", "Trialing"
        ACTIVE = "ACTIVE", "Active"
        PAST_DUE = "PAST_DUE", "Past Due"
        CANCELED = "CANCELED", "Canceled"
        UNPAID = "UNPAID", "Unpaid"
        PAUSED = "PAUSED", "Paused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.CharField(max_length=32, choices=PlanEnum.choices, default=PlanEnum.FREE)
    status = models.CharField(max_length=32, choices=StatusEnum.choices, default=StatusEnum.NONE)
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True)
    stripe_price_id = models.CharField(max_length=255, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.organization.name}: {self.get_plan_display()} ({self.get_status_display()})"


class StripeWebhookEvent(models.Model):
    class ProcessingStatusEnum(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSED = "PROCESSED", "Processed"
        IGNORED = "IGNORED", "Ignored"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=255)
    payload_sha256 = models.CharField(max_length=64)
    status = models.CharField(
        max_length=32,
        choices=ProcessingStatusEnum.choices,
        default=ProcessingStatusEnum.RECEIVED,
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.event_type}: {self.stripe_event_id}"

