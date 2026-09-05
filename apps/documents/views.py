from __future__ import annotations

import fitz
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema

from apps.signing.models import SigningRequest
from services.pdf_engine import PDFEngine
from tasks.notifications import send_invitation_email_for_request
from tasks.notifications import send_admin_account_created, send_admin_password_changed
from utils.task_dispatch import enqueue_task

from .auth import HasValidSignacoreSecret, get_admin_actor, require_superuser_actor
from .models import AdminAuditLog, Document, DocumentField
from .serializers import (
    AdminAuditLogSerializer,
    AdminDocumentDetailSerializer,
    AdminDocumentListSerializer,
    AdminLoginSerializer,
    AdminPasswordChangeSerializer,
    AdminUserCreateSerializer,
    AdminUserSerializer,
    DocumentSendSerializer,
    DocumentFieldSerializer,
    DocumentFieldUpdateSerializer,
    DocumentUpdateSerializer,
    DocumentUploadSerializer,
    ManualDocumentFieldCreateSerializer,
)


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


def get_request_ip(request) -> str:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def get_admin_login_url() -> str:
    return f"{settings.SIGNING_LINK_BASE_URL.rstrip('/')}/admin/login"


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
    resolved_actor = actor if actor is not None else get_admin_actor(request)
    return AdminAuditLog.objects.create(
        actor=resolved_actor,
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
    with fitz.open(document.original_pdf.path) as pdf_document:
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


def build_document_detection_summary(document: Document) -> dict[str, str | int]:
    first_field = document.fields.order_by("page", "order").first()
    return {
        "source": first_field.detection_source if first_field else DocumentField.DetectionSourceEnum.HEURISTIC,
        "field_count": document.fields.count(),
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

        users = get_user_model().objects.filter(is_staff=True).order_by("username")
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
        with transaction.atomic():
            user = serializer.save()
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

        user = get_object_or_404(get_user_model(), pk=user_id, is_staff=True)
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
        actor = require_superuser_actor(request)
        if not actor:
            return Response({"detail": "Superuser access is required."}, status=status.HTTP_403_FORBIDDEN)

        logs = AdminAuditLog.objects.select_related("actor").all()[:100]
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
        documents = (
            Document.objects.all()
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
            actor = get_admin_actor(request) or get_signacore_service_user()
            document = Document.objects.create(
                title=title,
                original_pdf=pdf_file,
                created_by=actor,
            )
            detected_fields = engine.analyse(document.original_pdf.path)
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
                metadata={"field_count": len(detected_fields)},
            )

        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        payload = serialize_document_detail(document)
        payload["page_count"] = page_count
        payload["detection_summary"] = {
            "source": detected_fields[0].detection_source if detected_fields else DocumentField.DetectionSourceEnum.HEURISTIC,
            "field_count": len(detected_fields),
        }
        return Response(payload, status=status.HTTP_201_CREATED)


class AdminDocumentDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminDocumentDetailSerializer

    @extend_schema(operation_id="admin_documents_get")
    def get(self, request, document_id):
        document = get_object_or_404(Document.objects.prefetch_related("fields", "signing_requests"), pk=document_id)
        log_admin_event(
            request,
            AdminAuditLog.ActionEnum.DOCUMENT_VIEW,
            f"Viewed document: {document.title}.",
            target_type="document",
            target_id=document.id,
        )
        return Response(serialize_document_detail(document), status=status.HTTP_200_OK)

    def patch(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id)
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


class AdminDocumentFieldsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = ManualDocumentFieldCreateSerializer

    def post(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id)
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
        field = get_object_or_404(DocumentField, pk=field_id, document_id=document_id)
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
        field = get_object_or_404(DocumentField, pk=field_id, document_id=document_id)
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
        document = get_object_or_404(Document.objects.prefetch_related("fields", "signing_requests"), pk=document_id)
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
                    expires_at=expiry,
                )
                for item in serializer.validated_data["signers"]
            ]
            signed_request_exists = document.signing_requests.filter(
                status=SigningRequest.StatusEnum.SIGNED
            ).exists()
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
        document = get_object_or_404(Document, pk=document_id)
        with fitz.open(document.original_pdf.path) as pdf_document:
            if page_number < 1 or page_number > pdf_document.page_count:
                return Response({"detail": "Page not found."}, status=status.HTTP_404_NOT_FOUND)
            page = pdf_document[page_number - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
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
        document = get_object_or_404(Document.objects.prefetch_related("signing_requests", "fields"), pk=document_id)
        signing_request = get_object_or_404(
            SigningRequest.objects.prefetch_related("submissions"),
            pk=signing_request_id,
            document_id=document_id,
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

            has_other_signed_requests = document.signing_requests.exclude(pk=signing_request.pk).filter(
                status=SigningRequest.StatusEnum.SIGNED
            ).exists()
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
        document = get_object_or_404(Document.objects.prefetch_related("signing_requests"), pk=document_id)
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
            metadata={"reason": reason},
        )
        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        return Response(AdminDocumentDetailSerializer(document).data, status=status.HTTP_200_OK)


class AdminDocumentDownloadView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AdminDocumentDetailSerializer

    def get(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id)
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
