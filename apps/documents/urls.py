from django.urls import path

from .views import (
    AdminDocumentDetailView,
    AdminDocumentFieldDetailView,
    AdminDocumentFieldsView,
    AdminDocumentDownloadView,
    AdminDocumentSendView,
    AdminDocumentVoidView,
    AdminDocumentsView,
)


urlpatterns = [
    path("documents/", AdminDocumentsView.as_view(), name="admin-documents"),
    path("documents/<uuid:document_id>/", AdminDocumentDetailView.as_view(), name="admin-document-detail"),
    path("documents/<uuid:document_id>/void/", AdminDocumentVoidView.as_view(), name="admin-document-void"),
    path("documents/<uuid:document_id>/download/", AdminDocumentDownloadView.as_view(), name="admin-document-download"),
    path("documents/<uuid:document_id>/send/", AdminDocumentSendView.as_view(), name="admin-document-send"),
    path("documents/<uuid:document_id>/fields/", AdminDocumentFieldsView.as_view(), name="admin-document-fields"),
    path(
        "documents/<uuid:document_id>/fields/<uuid:field_id>/",
        AdminDocumentFieldDetailView.as_view(),
        name="admin-document-field-detail",
    ),
]
