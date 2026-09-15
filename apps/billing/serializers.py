from rest_framework import serializers

from .entitlements import get_entitlement_snapshot
from .models import BillingPlanConfiguration, OrganizationSubscription


class BillingPlanConfigurationSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="get_plan_display", read_only=True)
    interval_label = serializers.CharField(source="get_billing_interval_display", read_only=True)

    class Meta:
        model = BillingPlanConfiguration
        fields = ("plan", "name", "amount", "currency", "billing_interval", "interval_label")
        read_only_fields = fields


class OrganizationSubscriptionSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    can_create_checkout = serializers.SerializerMethodField()
    can_manage_billing = serializers.SerializerMethodField()
    available_plans = serializers.SerializerMethodField()
    seat_count = serializers.SerializerMethodField()
    entitlements = serializers.SerializerMethodField()

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
            "available_plans",
            "seat_count",
            "entitlements",
        )
        read_only_fields = fields

    def get_can_create_checkout(self, obj: OrganizationSubscription) -> bool:
        return obj.status not in {
            OrganizationSubscription.StatusEnum.ACTIVE,
            OrganizationSubscription.StatusEnum.TRIALING,
        }

    def get_can_manage_billing(self, obj: OrganizationSubscription) -> bool:
        return bool(obj.stripe_customer_id)

    def get_available_plans(self, obj: OrganizationSubscription) -> list[dict]:
        plans = BillingPlanConfiguration.objects.filter(
            is_active=True,
            plan__in=(
                BillingPlanConfiguration.PlanEnum.PROFESSIONAL,
                BillingPlanConfiguration.PlanEnum.BUSINESS,
            ),
        )
        return BillingPlanConfigurationSerializer(plans, many=True).data

    def get_seat_count(self, obj: OrganizationSubscription) -> int:
        return obj.organization.memberships.filter(
            status="ACTIVE",
            user__is_active=True,
        ).count()

    def get_entitlements(self, obj: OrganizationSubscription) -> dict:
        return get_entitlement_snapshot(
            obj.organization,
            actor=self.context.get("actor"),
            subscription=obj,
        )


class CheckoutSessionSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(
        choices=(
            OrganizationSubscription.PlanEnum.PROFESSIONAL,
            OrganizationSubscription.PlanEnum.BUSINESS,
        )
    )
    billing_interval = serializers.ChoiceField(
        choices=BillingPlanConfiguration.BillingIntervalEnum.choices,
        default=BillingPlanConfiguration.BillingIntervalEnum.YEAR,
    )


class CheckoutSessionResponseSerializer(serializers.Serializer):
    checkout_url = serializers.URLField()


class BillingPortalResponseSerializer(serializers.Serializer):
    portal_url = serializers.URLField()


class StripeWebhookResponseSerializer(serializers.Serializer):
    received = serializers.BooleanField()
    duplicate = serializers.BooleanField(required=False)
