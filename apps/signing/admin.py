from django.contrib import admin

from .models import FieldSubmission, SigningRequest


@admin.register(SigningRequest)
class SigningRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "status", "signed_at", "expires_at")
    list_filter = ("status",)
    search_fields = ("id",)


@admin.register(FieldSubmission)
class FieldSubmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "signing_request", "document_field", "value_type", "submitted_at")
    list_filter = ("value_type",)

