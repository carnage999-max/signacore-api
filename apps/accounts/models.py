import uuid

from django.conf import settings
from django.db import models

from utils.encryption import EncryptedEmailField, EncryptedTextField
from utils.identity import email_digest


class OAuthIntentEnum(models.TextChoices):
    LOGIN = "LOGIN", "Log in"
    REGISTER = "REGISTER", "Register"


class AccountProfile(models.Model):
    class AccountTypeEnum(models.TextChoices):
        PLATFORM = "PLATFORM", "Platform"
        COMPANY = "COMPANY", "Company"
        SIGNER = "SIGNER", "Signer"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="signacore_profile",
    )
    account_type = models.CharField(max_length=32, choices=AccountTypeEnum.choices)
    email = EncryptedEmailField(blank=True)
    email_hash = models.CharField(max_length=64, db_index=True, editable=False, default="")
    display_name = EncryptedTextField(blank=True)
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("email_hash",),
                condition=~models.Q(email_hash=""),
                name="unique_signacore_account_email_hash",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user.username} ({self.get_account_type_display()})"

    def save(self, *args, **kwargs):
        self.email_hash = email_digest(self.email) if self.email else ""
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "email" in update_fields:
            kwargs["update_fields"] = set(update_fields) | {"email_hash"}
        return super().save(*args, **kwargs)


class Organization(models.Model):
    class StatusEnum(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=32, choices=StatusEnum.choices, default=StatusEnum.ACTIVE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="signacore_organizations_created",
    )
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "created_at")

    def __str__(self) -> str:
        return self.name


class OrganizationMembership(models.Model):
    class RoleEnum(models.TextChoices):
        OWNER = "OWNER", "Owner"
        ADMIN = "ADMIN", "Admin"
        MEMBER = "MEMBER", "Member"

    class StatusEnum(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INVITED = "INVITED", "Invited"
        SUSPENDED = "SUSPENDED", "Suspended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="signacore_memberships",
    )
    role = models.CharField(max_length=32, choices=RoleEnum.choices)
    status = models.CharField(max_length=32, choices=StatusEnum.choices, default=StatusEnum.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "user"),
                name="unique_signacore_organization_membership",
            )
        ]
        ordering = ("organization__name", "user__username")

    def __str__(self) -> str:
        return f"{self.user.username} - {self.organization.name} ({self.get_role_display()})"


class OrganizationInvitation(models.Model):
    class RoleEnum(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        MEMBER = "MEMBER", "Member"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="signacore_invitations_sent",
    )
    email = EncryptedEmailField()
    email_hash = models.CharField(max_length=64, db_index=True, editable=False)
    role = models.CharField(max_length=32, choices=RoleEnum.choices, default=RoleEnum.MEMBER)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.email} invited to {self.organization.name}"

    def save(self, *args, **kwargs):
        self.email_hash = email_digest(self.email)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"email_hash"}
        return super().save(*args, **kwargs)


class SocialIdentity(models.Model):
    class ProviderEnum(models.TextChoices):
        GOOGLE = "GOOGLE", "Google"
        APPLE = "APPLE", "Apple"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="signacore_social_identities",
    )
    provider = models.CharField(max_length=32, choices=ProviderEnum.choices)
    subject_hash = models.CharField(max_length=64)
    subject = EncryptedTextField()
    email = EncryptedEmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "subject_hash"),
                name="unique_signacore_social_identity",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_provider_display()} identity for {self.user.username}"
