from rest_framework import serializers

from .models import OrganizationSubscription


class OrganizationSubscriptionSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    can_create_checkout = serializers.SerializerMethodField()
    can_manage_billing = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationSubscription
        fields = (
            "organization",
            "organization_name",
            "plan",
            "status",
            "current_period_end",
            "cancel_at_period_end",
            "can_create_checkout",
            "can_manage_billing",
        )
        read_only_fields = fields

    def get_can_create_checkout(self, obj: OrganizationSubscription) -> bool:
        return obj.status not in {
            OrganizationSubscription.StatusEnum.ACTIVE,
            OrganizationSubscription.StatusEnum.TRIALING,
        }

    def get_can_manage_billing(self, obj: OrganizationSubscription) -> bool:
        return bool(obj.stripe_customer_id)


class CheckoutSessionSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(
        choices=(
            OrganizationSubscription.PlanEnum.PROFESSIONAL,
            OrganizationSubscription.PlanEnum.BUSINESS,
        )
    )


class CheckoutSessionResponseSerializer(serializers.Serializer):
    checkout_url = serializers.URLField()


class BillingPortalResponseSerializer(serializers.Serializer):
    portal_url = serializers.URLField()


class StripeWebhookResponseSerializer(serializers.Serializer):
    received = serializers.BooleanField()
    duplicate = serializers.BooleanField(required=False)
