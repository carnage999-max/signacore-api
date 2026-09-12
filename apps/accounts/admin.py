from django.contrib import admin

from .models import AccountProfile, Organization, OrganizationMembership, SocialIdentity


@admin.register(AccountProfile)
class AccountProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "account_type", "onboarding_completed_at", "created_at")
    list_filter = ("account_type",)
    search_fields = ("user__username",)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "created_by", "onboarding_completed_at", "created_at")
    list_filter = ("status",)
    search_fields = ("name",)


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ("organization", "user", "role", "status", "created_at")
    list_filter = ("role", "status")
    search_fields = ("organization__name", "user__username")


@admin.register(SocialIdentity)
class SocialIdentityAdmin(admin.ModelAdmin):
    list_display = ("provider", "user", "last_login_at", "created_at")
    list_filter = ("provider",)
    search_fields = ("user__username", "subject_hash")
    readonly_fields = ("subject_hash", "subject", "email", "created_at", "last_login_at")
