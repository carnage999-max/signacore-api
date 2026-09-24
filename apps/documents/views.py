from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import fitz
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import AccountProfile, OrganizationMembership
from apps.billing.entitlements import PlanFeatureEnum, require_feature, require_monthly_document_capacity
from apps.signing.models import SigningRequest
from services.authored_pdf import AuthoredPDFRenderer
from services.docx_import import DocxImporter, DocxImportError
from services.pdf_engine import PDFEngine
from tasks.notifications import (
    send_admin_account_created,
    send_admin_password_changed,
    send_invitation_email_for_request,
)
from utils.file_storage import temporary_plaintext_file
from utils.identity import email_digest
from utils.pdf_preview import build_preview_matrix, prepare_page_for_preview
from utils.task_dispatch import enqueue_task
from utils.throttling import SignacoreRateThrottle

from .auth import (
    HasValidSignacoreSecret,
    get_actor_organization,
    get_admin_actor,
    require_superuser_actor,
)
from .models import AdminAuditLog, Document, DocumentField
from .serializers import (
    AdminAuditLogSerializer,
    AdminDocumentDetailSerializer,
    AdminDocumentListSerializer,
    AdminLoginSerializer,
    AdminPasswordChangeSerializer,
    AdminUserCreateSerializer,
    AdminUserSerializer,
    AuthoredDocumentSerializer,
    DocumentFieldSerializer,
    DocumentFieldUpdateSerializer,
    DocumentSendSerializer,
    DocumentUpdateSerializer,
    DocumentUploadSerializer,
    DocxImportSerializer,
    ManualDocumentFieldCreateSerializer,
)

logger = logging.getLogger(__name__)


def get_signacore_service_user():
    user_model = get_user_model()
    user, _ = user_model.objects.get_or_create(
        username=settings.SIGNACORE_SERVICE_USERNAME,
        defaults={
            "is_staff": True,
            "is_active": True,
        },
    )
    return user


def get_request_actor_and_organization(request):
    actor = get_admin_actor(request) or get_signacore_service_user()
    organization = get_actor_organization(request, actor)
    if organization is None:
        raise PermissionDenied("No active company workspace is available.")
    return actor, organization


def get_scoped_document(request, document_id, *, prefetch: tuple[str, ...] = ()) -> Document:
    _, organization = get_request_actor_and_organization(request)
    queryset = Document.objects.filter(organization=organization)
    if prefetch:
        queryset = queryset.prefetch_related(*prefetch)
    return get_object_or_404(queryset, pk=document_id)


def get_request_ip(request) -> str:
    real_ip = request.META.get("HTTP_X_REAL_IP", "").strip()
    if real_ip:
        return real_ip
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded_for.split(",")[0].strip() if forwarded_for else request.META.get("REMOTE_ADDR", "")


def get_admin_login_url() -> str:
    return f"{settings.SIGNACORE_APP_URL.rstrip('/')}/admin/login"


def log_admin_event(
    request,
    action: str,
    summary: str,
    *,
    actor=None,
    target_type: str = "",
    target_id: str = "",
    metadata: dict | None = None,
) -> AdminAuditLog:
    resolved_actor = actor if actor is not None else (get_admin_actor(request) or get_signacore_service_user())
    organization = get_actor_organization(request, resolved_actor) if resolved_actor else None
    return AdminAuditLog.objects.create(
        actor=resolved_actor,
        organization=organization,
        actor_email=getattr(resolved_actor, "email", "") or "",
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else "",
        summary=summary,
        metadata=metadata or {},
        ip_address=get_request_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )


def build_document_page_payload(document: Document) -> list[dict[str, float | int | str]]:
    pages: list[dict[str, float | int | str]] = []
    with temporary_plaintext_file(document.original_pdf, suffix=".pdf") as pdf_path:
        with fitz.open(pdf_path) as pdf_document:
            for page_number, page in enumerate(pdf_document, start=1):
                pages.append(
                    {
                        "number": page_number,
                        "width": float(page.rect.width),
                        "height": float(page.rect.height),
                        "preview_url": f"/api/admin/documents/{document.id}/pages/{page_number}/preview/",
                    }
                )
    return pages


def build_document_detection_summary(document: Document) -> dict[str, Any]:
    """Keep the original ``source``/``field_count`` contract and extend it with the import report."""
    report = document.import_report or {}
    first_field = document.fields.order_by("page", "order").first()
    field_count = document.fields.count()
    if first_field is not None:
        source = first_field.detection_source
    else:
        source = report.get("source") or DocumentField.DetectionSourceEnum.HEURISTIC
    return {
        "source": source,
        "field_count": field_count,
        "native_widget_count": report.get("native_widget_count", 0),
        "imported_field_count": report.get("imported_field_count", field_count),
        "ignored_widget_count": report.get("ignored_widget_count", 0),
        "warning_codes": report.get("warning_codes", []),
    }


def serialize_document_detail(document: Document) -> dict:
    serializer = AdminDocumentDetailSerializer(document)
    pages = build_document_page_payload(document)
    return {
        **serializer.data,
        "page_count": len(pages),
        "pages": pages,
        "detection_summary": build_document_detection_summary(document),
    }


class AdminAuthLoginView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "admin_auth"
    serializer_class = AdminLoginSerializer

    def post(self, request):
        serializer = AdminLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.LOGIN,
            f"Signed in as {user.username}.",
            actor=user,
            target_type="admin_user",
            target_id=user.id,
        )
        return Response({"admin": AdminUserSerializer(user).data}, status=status.HTTP_200_OK)


class AdminAuthLogoutView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminUserSerializer

    def post(self, request):
        actor = get_admin_actor(request)
        if actor:
            log_admin_event(
                request,
                AdminAuditLog.ActionEnum.LOGOUT,
                f"Signed out as {actor.username}.",
                actor=actor,
                target_type="admin_user",
                target_id=actor.id,
            )
        return Response({"detail": "Signed out."}, status=status.HTTP_200_OK)


class AdminUsersView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminUserCreateSerializer

    def get(self, request):
        actor = require_superuser_actor(request)
        if not actor:
            return Response({"detail": "Superuser access is required."}, status=status.HTTP_403_FORBIDDEN)

        organization = get_actor_organization(request, actor)
        if organization is None:
            raise PermissionDenied("No active company workspace is available.")
        users = (
            get_user_model()
            .objects.filter(
                is_staff=True,
                signacore_memberships__organization=organization,
                signacore_memberships__status=OrganizationMembership.StatusEnum.ACTIVE,
            )
            .distinct()
            .order_by("username")
        )
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.ADMIN_USER_LIST,
            "Viewed admin users.",
            actor=actor,
        )
        return Response({"items": AdminUserSerializer(users, many=True).data}, status=status.HTTP_200_OK)

    def post(self, request):
        actor = require_superuser_actor(request)
        if not actor:
            return Response({"detail": "Superuser access is required."}, status=status.HTTP_403_FORBIDDEN)

        serializer = AdminUserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = get_actor_organization(request, actor)
        if organization is None:
            raise PermissionDenied("No active company workspace is available.")
        with transaction.atomic():
            user = serializer.save()
            OrganizationMembership.objects.create(
                organization=organization,
                user=user,
                role=(
                    OrganizationMembership.RoleEnum.OWNER
                    if user.is_superuser
                    else OrganizationMembership.RoleEnum.ADMIN
                ),
            )
            AccountProfile.objects.create(
                user=user,
                account_type=(
                    AccountProfile.AccountTypeEnum.PLATFORM
                    if user.is_superuser
                    else AccountProfile.AccountTypeEnum.COMPANY
                ),
                email=user.email,
                display_name=user.get_full_name() or user.username,
            )
            log_admin_event(
                request,
                AdminAuditLog.ActionEnum.ADMIN_USER_CREATE,
                f"Created admin user: {user.username}.",
                actor=actor,
                target_type="admin_user",
                target_id=user.id,
                metadata={"is_superuser": user.is_superuser},
            )

        if user.email:
            enqueue_task(
                send_admin_account_created,
                user.email,
                user.username,
                user.temporary_password,
                get_admin_login_url(),
            )

        return Response({"admin": AdminUserSerializer(user).data}, status=status.HTTP_201_CREATED)


class AdminUserPasswordView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminPasswordChangeSerializer

    def post(self, request, user_id):
        actor = require_superuser_actor(request)
        if not actor:
            return Response({"detail": "Superuser access is required."}, status=status.HTTP_403_FORBIDDEN)

        organization = get_actor_organization(request, actor)
        if organization is None:
            raise PermissionDenied("No active company workspace is available.")
        user = get_object_or_404(
            get_user_model(),
            pk=user_id,
            is_staff=True,
            signacore_memberships__organization=organization,
            signacore_memberships__status=OrganizationMembership.StatusEnum.ACTIVE,
        )
        serializer = AdminPasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        temporary_password = serializer.validated_data["temporary_password"]
        user.set_password(temporary_password)
        user.save(update_fields=["password"])
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.ADMIN_PASSWORD_CHANGE,
            f"Changed password for admin user: {user.username}.",
            actor=actor,
            target_type="admin_user",
            target_id=user.id,
        )

        if user.email:
            enqueue_task(
                send_admin_password_changed,
                user.email,
                user.username,
                temporary_password,
                get_admin_login_url(),
            )

        return Response({"admin": AdminUserSerializer(user).data}, status=status.HTTP_200_OK)


class AdminAuditLogsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminAuditLogSerializer

    def get(self, request):
        actor, organization = get_request_actor_and_organization(request)
        if organization is None:
            raise PermissionDenied("No active company workspace is available.")
        if (
            not actor.is_superuser
            and not OrganizationMembership.objects.filter(
                organization=organization,
                user=actor,
                status=OrganizationMembership.StatusEnum.ACTIVE,
                role__in=(
                    OrganizationMembership.RoleEnum.OWNER,
                    OrganizationMembership.RoleEnum.ADMIN,
                ),
            ).exists()
        ):
            raise PermissionDenied("Workspace administrator access is required.")
        require_feature(actor, organization, PlanFeatureEnum.AUDIT_HISTORY)
        logs = AdminAuditLog.objects.select_related("actor").filter(organization=organization)[:100]
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.AUDIT_LOG_LIST,
            "Viewed admin audit logs.",
            actor=actor,
        )
        return Response({"items": AdminAuditLogSerializer(logs, many=True).data}, status=status.HTTP_200_OK)


class AdminDocumentsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    parser_classes = [MultiPartParser, FormParser]
    serializer_class = DocumentUploadSerializer

    @extend_schema(operation_id="admin_documents_list")
    def get(self, request):
        _, organization = get_request_actor_and_organization(request)
        documents = (
            Document.objects.filter(organization=organization)
            .annotate(
                signer_count=Count("signing_requests", distinct=True),
                signed_count=Count(
                    "signing_requests",
                    filter=Q(signing_requests__status="SIGNED"),
                    distinct=True,
                ),
            )
            .order_by("-created_at")
        )
        serializer = AdminDocumentListSerializer(documents, many=True)
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_LIST,
            "Viewed document list.",
        )
        return Response({"items": serializer.data}, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = DocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        actor, organization = get_request_actor_and_organization(request)
        require_monthly_document_capacity(actor, organization, operation="create")

        title = serializer.validated_data["title"]
        pdf_file = serializer.validated_data["pdf_file"]
        engine = PDFEngine()

        try:
            with fitz.open(stream=pdf_file.read(), filetype="pdf") as pdf_document:
                page_count = pdf_document.page_count
        except Exception as exc:
            raise ValidationError({"pdf_file": ["Invalid PDF file."]}) from exc
        finally:
            pdf_file.seek(0)

        with transaction.atomic():
            document = Document.objects.create(
                title=title,
                original_pdf=pdf_file,
                created_by=actor,
                organization=organization,
            )
            with temporary_plaintext_file(document.original_pdf, suffix=".pdf") as pdf_path:
                import_result = engine.analyse(pdf_path)
            detected_fields = import_result.fields
            document.import_report = import_result.report.as_dict()
            document.save(update_fields=["import_report", "updated_at"])
            DocumentField.objects.bulk_create(
                [
                    DocumentField(
                        document=document,
                        field_type=field.field_type,
                        label=field.label,
                        page=field.page,
                        x=field.x,
                        y=field.y,
                        width=field.width,
                        height=field.height,
                        is_required=field.is_required,
                        detection_source=field.detection_source,
                        order=field.order,
                        max_length=field.max_length,
                        is_comb=field.is_comb,
                    )
                    for field in detected_fields
                ]
            )
            log_admin_event(
                request,
                AdminAuditLog.ActionEnum.DOCUMENT_UPLOAD,
                f"Uploaded document: {document.title}.",
                actor=actor,
                target_type="document",
                target_id=document.id,
                metadata={
                    "field_count": len(detected_fields),
                    "ignored_widget_count": import_result.report.ignored_widget_count,
                },
            )

        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        payload = serialize_document_detail(document)
        payload["page_count"] = page_count
        return Response(payload, status=status.HTTP_201_CREATED)


class AdminAuthoredDocumentView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AuthoredDocumentSerializer

    def post(self, request):
        actor, organization = get_request_actor_and_organization(request)
        require_monthly_document_capacity(actor, organization, operation="create")
        serializer = AuthoredDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        content = serializer.validated_data["content"]
        pdf_bytes, rendered_fields = AuthoredPDFRenderer().render(content)
        with transaction.atomic():
            document = Document.objects.create(
                title=serializer.validated_data["title"],
                source=Document.SourceEnum.AUTHORED,
                authored_content=json.dumps(content, separators=(",", ":")),
                original_pdf=ContentFile(pdf_bytes, name=f"authored-{uuid.uuid4()}.pdf"),
                created_by=actor,
                organization=organization,
            )
            DocumentField.objects.bulk_create(
                [
                    DocumentField(
                        document=document,
                        field_type=field.field_type,
                        label=field.label,
                        page=field.page,
                        x=field.x,
                        y=field.y,
                        width=field.width,
                        height=field.height,
                        is_required=field.is_required,
                        detection_source=DocumentField.DetectionSourceEnum.AUTHORED,
                        order=field.order,
                    )
                    for field in rendered_fields
                ]
            )
            log_admin_event(
                request,
                AdminAuditLog.ActionEnum.DOCUMENT_AUTHOR,
                f"Created authored document: {document.title}.",
                actor=actor,
                target_type="document",
                target_id=document.id,
                metadata={"field_count": len(rendered_fields)},
            )

        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        return Response(serialize_document_detail(document), status=status.HTTP_201_CREATED)


class AdminAuthoredDocumentImportView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "document_import"
    serializer_class = DocxImportSerializer

    def post(self, request):
        actor, organization = get_request_actor_and_organization(request)
        require_feature(actor, organization, PlanFeatureEnum.DOCUMENT_CREATE)
        serializer = DocxImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        docx_file = serializer.validated_data["docx_file"]
        try:
            result = DocxImporter().import_bytes(docx_file.read(), docx_file.name)
        except DocxImportError as exc:
            raise ValidationError({"docx_file": [str(exc)]}) from exc
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_IMPORT,
            "Imported a DOCX into the document editor.",
            actor=actor,
            metadata={"warning_count": len(result.warnings)},
        )
        return Response(
            {
                "title": result.title,
                "content": result.content,
                "warnings": result.warnings,
            },
            status=status.HTTP_200_OK,
        )


class AdminAuthoredDocumentDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AuthoredDocumentSerializer

    def get(self, request, document_id):
        document = get_scoped_document(request, document_id, prefetch=("fields", "signing_requests"))
        if document.source != Document.SourceEnum.AUTHORED:
            raise ValidationError({"document": ["Only authored documents can be opened here."]})
        try:
            content = json.loads(document.authored_content or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError({"document": ["This authored document has invalid saved content."]}) from exc
        return Response(
            {**serialize_document_detail(document), "content": content},
            status=status.HTTP_200_OK,
        )

    def patch(self, request, document_id):
        document = get_scoped_document(request, document_id)
        if document.source != Document.SourceEnum.AUTHORED:
            raise ValidationError({"document": ["Only authored documents can be edited here."]})
        if document.status != Document.StatusEnum.DRAFT:
            raise ValidationError({"document": ["Only draft authored documents can be edited."]})

        serializer = AuthoredDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        content = serializer.validated_data["content"]
        pdf_bytes, rendered_fields = AuthoredPDFRenderer().render(content)

        with transaction.atomic():
            if document.original_pdf:
                document.original_pdf.delete(save=False)
            document.title = serializer.validated_data["title"]
            document.authored_content = json.dumps(content, separators=(",", ":"))
            document.original_pdf = ContentFile(pdf_bytes, name=f"authored-{uuid.uuid4()}.pdf")
            document.save(update_fields=["title", "authored_content", "original_pdf", "updated_at"])
            document.fields.all().delete()
            DocumentField.objects.bulk_create(
                [
                    DocumentField(
                        document=document,
                        field_type=field.field_type,
                        label=field.label,
                        page=field.page,
                        x=field.x,
                        y=field.y,
                        width=field.width,
                        height=field.height,
                        is_required=field.is_required,
                        detection_source=DocumentField.DetectionSourceEnum.AUTHORED,
                        order=field.order,
                    )
                    for field in rendered_fields
                ]
            )
            log_admin_event(
                request,
                AdminAuditLog.ActionEnum.DOCUMENT_UPDATE,
                f"Updated authored document: {document.title}.",
                target_type="document",
                target_id=document.id,
                metadata={"field_count": len(rendered_fields)},
            )

        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        return Response(serialize_document_detail(document), status=status.HTTP_200_OK)


class AdminDocumentDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminDocumentDetailSerializer

    @extend_schema(operation_id="admin_documents_get")
    def get(self, request, document_id):
        document = get_scoped_document(request, document_id, prefetch=("fields", "signing_requests"))
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_VIEW,
            f"Viewed document: {document.title}.",
            target_type="document",
            target_id=document.id,
        )
        try:
            payload = serialize_document_detail(document)
        except Exception:
            logger.exception(
                "Admin document detail serialization failed",
                extra={
                    "document_id": str(document.id),
                    "original_pdf_name": document.original_pdf.name if document.original_pdf else "",
                },
            )
            return Response(
                {
                    "detail": "This document could not be opened because its PDF preview is unavailable. Try again shortly."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(payload, status=status.HTTP_200_OK)

    def patch(self, request, document_id):
        document = get_scoped_document(request, document_id)
        serializer = DocumentUpdateSerializer(document, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_UPDATE,
            f"Updated document: {document.title}.",
            target_type="document",
            target_id=document.id,
            metadata={"updated_fields": list(serializer.validated_data.keys())},
        )
        return Response(AdminDocumentDetailSerializer(document).data, status=status.HTTP_200_OK)

    def delete(self, request, document_id):
        document = get_scoped_document(request, document_id)
        document_title = document.title
        original_name = document.original_pdf.name if document.original_pdf else ""
        signed_name = document.signed_pdf.name if document.signed_pdf else ""
        original_storage = document.original_pdf.storage if document.original_pdf else None
        signed_storage = document.signed_pdf.storage if document.signed_pdf else None

        with transaction.atomic():
            document.delete()
            log_admin_event(
                request,
                AdminAuditLog.ActionEnum.DOCUMENT_DELETE,
                f"Deleted document: {document_title}.",
                target_type="document",
                target_id=document_id,
            )
            if original_storage and original_name:
                transaction.on_commit(lambda: original_storage.delete(original_name))
            if signed_storage and signed_name:
                transaction.on_commit(lambda: signed_storage.delete(signed_name))

        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminDocumentFieldsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = ManualDocumentFieldCreateSerializer

    def post(self, request, document_id):
        document = get_scoped_document(request, document_id)
        serializer = ManualDocumentFieldCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        field = serializer.save(
            document=document,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
        )
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.FIELD_CREATE,
            f"Added field: {field.label}.",
            target_type="document_field",
            target_id=field.id,
            metadata={"document_id": str(document.id), "field_type": field.field_type},
        )
        return Response(DocumentFieldSerializer(field).data, status=status.HTTP_201_CREATED)


class AdminDocumentFieldDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = DocumentFieldUpdateSerializer

    def patch(self, request, document_id, field_id):
        _, organization = get_request_actor_and_organization(request)
        field = get_object_or_404(
            DocumentField,
            pk=field_id,
            document_id=document_id,
            document__organization=organization,
        )
        serializer = DocumentFieldUpdateSerializer(field, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.FIELD_UPDATE,
            f"Updated field: {field.label}.",
            target_type="document_field",
            target_id=field.id,
            metadata={"document_id": str(document_id), "updated_fields": list(serializer.validated_data.keys())},
        )
        return Response(DocumentFieldSerializer(field).data, status=status.HTTP_200_OK)

    def delete(self, request, document_id, field_id):
        _, organization = get_request_actor_and_organization(request)
        field = get_object_or_404(
            DocumentField,
            pk=field_id,
            document_id=document_id,
            document__organization=organization,
        )
        field_label = field.label
        field.delete()
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.FIELD_DELETE,
            f"Removed field: {field_label}.",
            target_type="document_field",
            target_id=field_id,
            metadata={"document_id": str(document_id)},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminDocumentSendView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = DocumentSendSerializer

    def post(self, request, document_id):
        document = get_scoped_document(request, document_id, prefetch=("fields", "signing_requests"))
        actor, organization = get_request_actor_and_organization(request)
        require_monthly_document_capacity(actor, organization, operation="send", document=document)
        serializer = DocumentSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if document.status == Document.StatusEnum.VOIDED:
            return Response(
                {"status": ["Voided documents cannot be sent."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if document.status == Document.StatusEnum.COMPLETED:
            return Response(
                {"status": ["Completed documents must be re-opened per signer before sending again."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not document.fields.exists():
            return Response(
                {"fields": ["Add at least one field before sending."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing_emails = {
            signing_request.signer_email.strip().lower()
            for signing_request in document.signing_requests.all()
            if signing_request.status != SigningRequest.StatusEnum.EXPIRED
        }
        requested_emails = [item["signer_email"].strip().lower() for item in serializer.validated_data["signers"]]
        duplicate_emails = sorted({email for email in requested_emails if requested_emails.count(email) > 1})
        if duplicate_emails:
            return Response(
                {"signers": [f"Duplicate signer emails in request: {', '.join(duplicate_emails)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        already_present = sorted({email for email in requested_emails if email in existing_emails})
        if already_present:
            return Response(
                {"signers": [f"Signer already exists on this document: {', '.join(already_present)}."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        expiry = timezone.now() + timedelta(days=settings.SIGNING_LINK_EXPIRY_DAYS)
        with transaction.atomic():
            created_requests = [
                SigningRequest.objects.create(
                    document=document,
                    signer_email=item["signer_email"],
                    signer_name=item.get("signer_name") or "",
                    signer_user_id=(
                        AccountProfile.objects.filter(
                            email_hash=email_digest(item["signer_email"]),
                            account_type=AccountProfile.AccountTypeEnum.SIGNER,
                            user__is_active=True,
                        )
                        .values_list("user", flat=True)
                        .first()
                    ),
                    expires_at=expiry,
                )
                for item in serializer.validated_data["signers"]
            ]
            signed_request_exists = document.signing_requests.filter(status=SigningRequest.StatusEnum.SIGNED).exists()
            document.status = (
                Document.StatusEnum.PARTIALLY_SIGNED if signed_request_exists else Document.StatusEnum.SENT
            )
            document.save(update_fields=["status", "updated_at"])

        for signing_request in created_requests:
            enqueue_task(send_invitation_email_for_request, str(signing_request.id))
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.SIGNING_REQUEST_SEND,
            f"Sent document to {len(created_requests)} signer(s): {document.title}.",
            target_type="document",
            target_id=document.id,
            metadata={
                "signer_count": len(created_requests),
                "signer_request_ids": [str(signing_request.id) for signing_request in created_requests],
            },
        )
        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        payload = serialize_document_detail(document)
        payload["signer_count"] = len(created_requests)
        return Response(payload, status=status.HTTP_200_OK)


class AdminDocumentPagePreviewView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminDocumentDetailSerializer

    def get(self, request, document_id, page_number):
        document = get_scoped_document(request, document_id)
        try:
            with temporary_plaintext_file(document.original_pdf, suffix=".pdf") as pdf_path:
                with fitz.open(pdf_path) as pdf_document:
                    if page_number < 1 or page_number > pdf_document.page_count:
                        return Response({"detail": "Page not found."}, status=status.HTTP_404_NOT_FOUND)
                    page = pdf_document[page_number - 1]
                    prepare_page_for_preview(page)
                    matrix = build_preview_matrix(page, request.query_params.get("width"))
                    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        except Exception:
            logger.exception(
                "Admin document page preview failed",
                extra={
                    "document_id": str(document.id),
                    "page_number": page_number,
                    "original_pdf_name": document.original_pdf.name if document.original_pdf else "",
                },
            )
            return Response(
                {"detail": "This PDF page could not be previewed. Try again shortly."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_VIEW,
            f"Previewed page {page_number} for document: {document.title}.",
            target_type="document",
            target_id=document.id,
            metadata={"page": page_number},
        )
        return HttpResponse(pixmap.tobytes("png"), content_type="image/png")


class AdminSigningRequestResendView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminDocumentDetailSerializer

    def post(self, request, document_id, signing_request_id):
        document = get_scoped_document(request, document_id, prefetch=("signing_requests", "fields"))
        signing_request = get_object_or_404(
            SigningRequest.objects.prefetch_related("submissions"),
            pk=signing_request_id,
            document_id=document_id,
            document__organization=document.organization,
        )

        if document.status == Document.StatusEnum.VOIDED:
            return Response(
                {"status": ["Voided documents cannot be re-opened."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not document.fields.exists():
            return Response(
                {"fields": ["Add at least one field before resending."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        expiry = timezone.now() + timedelta(days=settings.SIGNING_LINK_EXPIRY_DAYS)
        with transaction.atomic():
            signing_request.submissions.all().delete()
            signing_request.status = SigningRequest.StatusEnum.PENDING
            signing_request.otp_hash = ""
            signing_request.otp_expires_at = None
            signing_request.signed_at = None
            signing_request.ip_address = ""
            signing_request.user_agent = ""
            signing_request.expires_at = expiry
            signing_request.save(
                update_fields=[
                    "status",
                    "otp_hash",
                    "otp_expires_at",
                    "signed_at",
                    "ip_address",
                    "user_agent",
                    "expires_at",
                    "updated_at",
                ]
            )

            if document.signed_pdf:
                document.signed_pdf.delete(save=False)
                document.signed_pdf = None

            has_other_signed_requests = (
                document.signing_requests.exclude(pk=signing_request.pk)
                .filter(status=SigningRequest.StatusEnum.SIGNED)
                .exists()
            )
            document.status = (
                Document.StatusEnum.PARTIALLY_SIGNED if has_other_signed_requests else Document.StatusEnum.SENT
            )
            document.save(update_fields=["status", "signed_pdf", "updated_at"])

        enqueue_task(send_invitation_email_for_request, str(signing_request.id))
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.SIGNING_REQUEST_RESEND,
            f"Re-sent signing request for: {document.title}.",
            target_type="signing_request",
            target_id=signing_request.id,
            metadata={"document_id": str(document.id)},
        )
        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        return Response(serialize_document_detail(document), status=status.HTTP_200_OK)


class AdminDocumentVoidView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = DocumentUpdateSerializer

    def post(self, request, document_id):
        document = get_scoped_document(request, document_id, prefetch=("signing_requests",))
        if document.status not in {Document.StatusEnum.SENT, Document.StatusEnum.PARTIALLY_SIGNED}:
            return Response(
                {"status": ["Only sent or partially signed documents can be voided."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = str(request.data.get("voided_reason", "")).strip()
        with transaction.atomic():
            document.status = Document.StatusEnum.VOIDED
            document.voided_reason = reason
            document.voided_at = timezone.now()
            document.save(update_fields=["status", "voided_reason", "voided_at", "updated_at"])
            document.signing_requests.filter(status=SigningRequest.StatusEnum.PENDING).update(
                expires_at=timezone.now(),
                updated_at=timezone.now(),
            )

        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_VOID,
            f"Voided document: {document.title}.",
            target_type="document",
            target_id=document.id,
            metadata={"reason_provided": bool(reason)},
        )
        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        return Response(AdminDocumentDetailSerializer(document).data, status=status.HTTP_200_OK)


class AdminDocumentDownloadView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminDocumentDetailSerializer

    def get(self, request, document_id):
        document = get_scoped_document(request, document_id)
        actor, organization = get_request_actor_and_organization(request)
        require_feature(actor, organization, PlanFeatureEnum.COMPLETED_ARCHIVE)
        if document.status != Document.StatusEnum.COMPLETED or not document.signed_pdf:
            return Response(
                {"status": ["Signed PDF is only available for completed documents."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_DOWNLOAD,
            f"Downloaded signed PDF: {document.title}.",
            target_type="document",
            target_id=document.id,
        )
        return FileResponse(
            document.signed_pdf.open("rb"),
            content_type="application/pdf",
            as_attachment=True,
            filename=Path(document.signed_pdf.name).name,
        )
