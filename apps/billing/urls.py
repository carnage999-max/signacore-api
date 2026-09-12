from django.urls import path

from .views import BillingCheckoutView, BillingPortalView, BillingStatusView, StripeWebhookView


urlpatterns = [
    path("admin/billing/", BillingStatusView.as_view(), name="billing-status"),
    path("admin/billing/checkout/", BillingCheckoutView.as_view(), name="billing-checkout"),
    path("admin/billing/portal/", BillingPortalView.as_view(), name="billing-portal"),
    path("billing/webhooks/stripe/", StripeWebhookView.as_view(), name="stripe-webhook"),
]
