from django.contrib import admin

from .models import AdminAuditLog, Document, DocumentField


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "created_by", "created_at", "updated_at")
    search_fields = ("title",)
    list_filter = ("status",)


@admin.register(DocumentField)
class DocumentFieldAdmin(admin.ModelAdmin):
    list_display = ("label", "field_type", "document", "page", "is_required", "detection_source")
    search_fields = ("label",)
    list_filter = ("field_type", "detection_source", "is_required")


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "target_type", "target_id", "created_at")
    search_fields = ("summary", "target_id", "actor__username", "actor__email")
    list_filter = ("action", "target_type", "created_at")
    readonly_fields = (
        "id",
        "actor",
        "actor_email",
        "action",
        "target_type",
        "target_id",
        "summary",
        "metadata",
        "ip_address",
        "user_agent",
        "created_at",
    )
