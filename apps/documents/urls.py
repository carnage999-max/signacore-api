from django.urls import path

from .views import (
    AdminAuditLogsView,
    AdminAuthLoginView,
    AdminAuthLogoutView,
    AdminDocumentDetailView,
    AdminDocumentFieldDetailView,
    AdminDocumentFieldsView,
    AdminDocumentDownloadView,
    AdminDocumentPagePreviewView,
    AdminDocumentSendView,
    AdminSigningRequestResendView,
    AdminUserPasswordView,
    AdminUsersView,
    AdminDocumentVoidView,
    AdminDocumentsView,
)


urlpatterns = [
    path("auth/login/", AdminAuthLoginView.as_view(), name="admin-auth-login"),
    path("auth/logout/", AdminAuthLogoutView.as_view(), name="admin-auth-logout"),
    path("users/", AdminUsersView.as_view(), name="admin-users"),
    path("users/<int:user_id>/password/", AdminUserPasswordView.as_view(), name="admin-user-password"),
    path("audit-logs/", AdminAuditLogsView.as_view(), name="admin-audit-logs"),
    path("documents/", AdminDocumentsView.as_view(), name="admin-documents"),
    path("documents/<uuid:document_id>/", AdminDocumentDetailView.as_view(), name="admin-document-detail"),
    path("documents/<uuid:document_id>/void/", AdminDocumentVoidView.as_view(), name="admin-document-void"),
    path("documents/<uuid:document_id>/download/", AdminDocumentDownloadView.as_view(), name="admin-document-download"),
    path(
        "documents/<uuid:document_id>/pages/<int:page_number>/preview/",
        AdminDocumentPagePreviewView.as_view(),
        name="admin-document-page-preview",
    ),
    path("documents/<uuid:document_id>/send/", AdminDocumentSendView.as_view(), name="admin-document-send"),
    path(
        "documents/<uuid:document_id>/signing-requests/<uuid:signing_request_id>/resend/",
        AdminSigningRequestResendView.as_view(),
        name="admin-signing-request-resend",
    ),
    path("documents/<uuid:document_id>/fields/", AdminDocumentFieldsView.as_view(), name="admin-document-fields"),
    path(
        "documents/<uuid:document_id>/fields/<uuid:field_id>/",
        AdminDocumentFieldDetailView.as_view(),
        name="admin-document-field-detail",
    ),
]
