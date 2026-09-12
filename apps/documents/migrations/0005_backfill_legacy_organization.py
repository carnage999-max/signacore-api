from django.conf import settings
from django.db import migrations


def backfill_legacy_organization(apps, schema_editor):
    user_model = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    account_profile = apps.get_model("accounts", "AccountProfile")
    organization = apps.get_model("accounts", "Organization")
    membership = apps.get_model("accounts", "OrganizationMembership")
    document = apps.get_model("documents", "Document")
    audit_log = apps.get_model("documents", "AdminAuditLog")

    staff_users = list(user_model.objects.filter(is_staff=True).order_by("id"))
    owner = next((user for user in staff_users if user.is_superuser), None)
    if owner is None:
        owner = document.objects.order_by("created_at").values_list("created_by", flat=True).first()
        owner = user_model.objects.filter(pk=owner).first() if owner else None

    if owner is None:
        return

    legacy_organization = organization.objects.create(
        name="Se7en Inc.",
        status="ACTIVE",
        created_by=owner,
    )

    for user in staff_users:
        display_name = f"{user.first_name} {user.last_name}".strip() or user.username
        membership.objects.create(
            organization=legacy_organization,
            user=user,
            role="OWNER" if user.is_superuser else "ADMIN",
            status="ACTIVE",
        )
        account_profile.objects.get_or_create(
            user=user,
            defaults={
                "account_type": "PLATFORM" if user.is_superuser else "COMPANY",
                "email": user.email or "",
                "display_name": display_name,
            },
        )

    document.objects.filter(organization__isnull=True).update(organization=legacy_organization)
    audit_log.objects.filter(organization__isnull=True).update(organization=legacy_organization)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("documents", "0004_adminauditlog_organization_document_organization"),
    ]

    operations = [
        migrations.RunPython(backfill_legacy_organization, migrations.RunPython.noop),
    ]
