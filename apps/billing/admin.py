from django.contrib import admin

from .models import OrganizationSubscription, StripeWebhookEvent


@admin.register(OrganizationSubscription)
class OrganizationSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("organization", "plan", "status", "current_period_end", "updated_at")
    list_filter = ("plan", "status", "cancel_at_period_end")
    search_fields = ("organization__name", "stripe_customer_id", "stripe_subscription_id")


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "stripe_event_id", "status", "processed_at", "created_at")
    list_filter = ("status", "event_type")
    search_fields = ("stripe_event_id",)
    readonly_fields = (
        "id",
        "stripe_event_id",
        "event_type",
        "payload_sha256",
        "status",
        "processed_at",
        "created_at",
    )

