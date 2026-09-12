from django.contrib import admin

from .models import BillingPlanConfiguration, OrganizationSubscription, StripeWebhookEvent


@admin.register(BillingPlanConfiguration)
class BillingPlanConfigurationAdmin(admin.ModelAdmin):
    list_display = ("plan", "amount", "currency", "billing_interval", "is_active", "updated_at")
    list_editable = ("amount", "currency", "billing_interval", "is_active")
    list_filter = ("is_active", "currency", "billing_interval")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


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
