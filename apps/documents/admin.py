from django.contrib import admin

from .models import Document, DocumentField


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

