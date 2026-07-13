from __future__ import annotations

import fitz
from datetime import timedelta
from pathlib import Path

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

from apps.signing.models import SigningRequest
from services.pdf_engine import PDFEngine
from signacore_api.settings import SIGNACORE_SERVICE_USERNAME, SIGNING_LINK_EXPIRY_DAYS
from tasks.notifications import send_invitation_email_for_request
from utils.task_dispatch import enqueue_task

from .auth import HasValidSignacoreSecret
from .models import Document, DocumentField
from .serializers import (
    AdminDocumentDetailSerializer,
    AdminDocumentListSerializer,
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
        username=SIGNACORE_SERVICE_USERNAME,
        defaults={
            "is_staff": True,
            "is_active": True,
        },
    )
    return user


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


class AdminDocumentsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    parser_classes = [MultiPartParser, FormParser]

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
            document = Document.objects.create(
                title=title,
                original_pdf=pdf_file,
                created_by=get_signacore_service_user(),
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

    def get(self, request, document_id):
        document = get_object_or_404(Document.objects.prefetch_related("fields", "signing_requests"), pk=document_id)
        return Response(serialize_document_detail(document), status=status.HTTP_200_OK)

    def patch(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id)
        serializer = DocumentUpdateSerializer(document, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AdminDocumentDetailSerializer(document).data, status=status.HTTP_200_OK)


class AdminDocumentFieldsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

    def post(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id)
        serializer = ManualDocumentFieldCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        field = serializer.save(
            document=document,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
        )
        return Response(DocumentFieldSerializer(field).data, status=status.HTTP_201_CREATED)


class AdminDocumentFieldDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

    def patch(self, request, document_id, field_id):
        field = get_object_or_404(DocumentField, pk=field_id, document_id=document_id)
        serializer = DocumentFieldUpdateSerializer(field, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(DocumentFieldSerializer(field).data, status=status.HTTP_200_OK)

    def delete(self, request, document_id, field_id):
        field = get_object_or_404(DocumentField, pk=field_id, document_id=document_id)
        field.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminDocumentSendView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

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

        expiry = timezone.now() + timedelta(days=SIGNING_LINK_EXPIRY_DAYS)
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
        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        payload = serialize_document_detail(document)
        payload["signer_count"] = len(created_requests)
        return Response(payload, status=status.HTTP_200_OK)


class AdminDocumentPagePreviewView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

    def get(self, request, document_id, page_number):
        document = get_object_or_404(Document, pk=document_id)
        with fitz.open(document.original_pdf.path) as pdf_document:
            if page_number < 1 or page_number > pdf_document.page_count:
                return Response({"detail": "Page not found."}, status=status.HTTP_404_NOT_FOUND)
            page = pdf_document[page_number - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        return HttpResponse(pixmap.tobytes("png"), content_type="image/png")


class AdminSigningRequestResendView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

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

        expiry = timezone.now() + timedelta(days=SIGNING_LINK_EXPIRY_DAYS)
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
        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        return Response(serialize_document_detail(document), status=status.HTTP_200_OK)


class AdminDocumentVoidView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

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

        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        return Response(AdminDocumentDetailSerializer(document).data, status=status.HTTP_200_OK)


class AdminDocumentDownloadView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]

    def get(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id)
        if document.status != Document.StatusEnum.COMPLETED or not document.signed_pdf:
            return Response(
                {"status": ["Signed PDF is only available for completed documents."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return FileResponse(
            document.signed_pdf.open("rb"),
            content_type="application/pdf",
            as_attachment=True,
            filename=Path(document.signed_pdf.name).name,
        )
