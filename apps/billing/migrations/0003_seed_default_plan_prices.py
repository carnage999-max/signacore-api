from decimal import Decimal

from django.db import migrations

DEFAULT_PLANS = (
    {
        "plan": "PROFESSIONAL",
        "amount": Decimal("29.00"),
        "currency": "USD",
        "billing_interval": "month",
        "is_active": True,
    },
    {
        "plan": "BUSINESS",
        "amount": Decimal("79.00"),
        "currency": "USD",
        "billing_interval": "month",
        "is_active": True,
    },
)


def seed_default_plan_prices(apps, schema_editor) -> None:
    billing_plan = apps.get_model("billing", "BillingPlanConfiguration")
    for defaults in DEFAULT_PLANS:
        billing_plan.objects.get_or_create(
            plan=defaults["plan"],
            defaults={key: value for key, value in defaults.items() if key != "plan"},
        )


def remove_default_plan_prices(apps, schema_editor) -> None:
    billing_plan = apps.get_model("billing", "BillingPlanConfiguration")
    for defaults in DEFAULT_PLANS:
        billing_plan.objects.filter(
            plan=defaults["plan"],
            amount=defaults["amount"],
            currency=defaults["currency"],
            billing_interval=defaults["billing_interval"],
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_billingplanconfiguration"),
    ]

    operations = [
        migrations.RunPython(seed_default_plan_prices, remove_default_plan_prices),
    ]
