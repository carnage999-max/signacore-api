from django.urls import path

from .views import (
    BillingCheckoutStatusView,
    BillingCheckoutView,
    BillingPortalView,
    BillingStatusView,
    PublicBillingPlanListView,
    StripeWebhookView,
)

urlpatterns = [
    path("billing/plans/", PublicBillingPlanListView.as_view(), name="billing-plan-list"),
    path("admin/billing/", BillingStatusView.as_view(), name="billing-status"),
    path("admin/billing/checkout/", BillingCheckoutView.as_view(), name="billing-checkout"),
    path("admin/billing/checkout-status/", BillingCheckoutStatusView.as_view(), name="billing-checkout-status"),
    path("admin/billing/portal/", BillingPortalView.as_view(), name="billing-portal"),
    path("billing/webhooks/stripe/", StripeWebhookView.as_view(), name="stripe-webhook"),
]
