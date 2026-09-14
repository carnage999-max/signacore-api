from decimal import Decimal

import django.core.validators
from django.db import migrations, models


DEFAULT_PLAN_PRICES = (
    {
        "plan": "FREE",
        "amount": Decimal("0.00"),
        "currency": "USD",
        "billing_interval": "month",
        "is_active": True,
    },
    {
        "plan": "PROFESSIONAL",
        "amount": Decimal("9.99"),
        "currency": "USD",
        "billing_interval": "month",
        "is_active": True,
    },
    {
        "plan": "PROFESSIONAL",
        "amount": Decimal("83.88"),
        "currency": "USD",
        "billing_interval": "year",
        "is_active": True,
    },
    {
        "plan": "BUSINESS",
        "amount": Decimal("19.99"),
        "currency": "USD",
        "billing_interval": "month",
        "is_active": True,
    },
    {
        "plan": "BUSINESS",
        "amount": Decimal("179.88"),
        "currency": "USD",
        "billing_interval": "year",
        "is_active": True,
    },
)


def seed_expanded_plan_prices(apps, schema_editor) -> None:
    billing_plan = apps.get_model("billing", "BillingPlanConfiguration")
    for defaults in DEFAULT_PLAN_PRICES:
        billing_plan.objects.update_or_create(
            plan=defaults["plan"],
            billing_interval=defaults["billing_interval"],
            defaults={
                key: value
                for key, value in defaults.items()
                if key not in {"plan", "billing_interval"}
            },
        )


def remove_expanded_plan_prices(apps, schema_editor) -> None:
    billing_plan = apps.get_model("billing", "BillingPlanConfiguration")
    for defaults in DEFAULT_PLAN_PRICES:
        billing_plan.objects.filter(
            plan=defaults["plan"],
            billing_interval=defaults["billing_interval"],
            amount=defaults["amount"],
            currency=defaults["currency"],
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0003_seed_default_plan_prices"),
    ]

    operations = [
        migrations.AlterField(
            model_name="billingplanconfiguration",
            name="plan",
            field=models.CharField(
                choices=[
                    ("FREE", "Free"),
                    ("PROFESSIONAL", "Professional"),
                    ("BUSINESS", "Business"),
                    ("ENTERPRISE", "Enterprise"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="billingplanconfiguration",
            name="amount",
            field=models.DecimalField(
                decimal_places=2,
                max_digits=10,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.AlterField(
            model_name="organizationsubscription",
            name="plan",
            field=models.CharField(
                choices=[
                    ("FREE", "Free"),
                    ("PROFESSIONAL", "Professional"),
                    ("BUSINESS", "Business"),
                    ("ENTERPRISE", "Enterprise"),
                ],
                default="FREE",
                max_length=32,
            ),
        ),
        migrations.AlterModelOptions(
            name="billingplanconfiguration",
            options={
                "ordering": ("plan", "billing_interval"),
                "verbose_name": "billing plan price",
                "verbose_name_plural": "billing plan prices",
            },
        ),
        migrations.AddConstraint(
            model_name="billingplanconfiguration",
            constraint=models.UniqueConstraint(
                fields=("plan", "billing_interval"),
                name="unique_billing_plan_interval",
            ),
        ),
        migrations.RunPython(seed_expanded_plan_prices, remove_expanded_plan_prices),
    ]
