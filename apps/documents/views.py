from __future__ import annotations

import fitz
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.signing.models import SigningRequest
from services.pdf_engine import PDFEngine
from signacore_api.settings import SIGNING_LINK_EXPIRY_DAYS

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

class AdminDocumentsView(APIView):
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
                created_by=request.user,
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

        response_serializer = AdminDocumentDetailSerializer(document)
        detection_source = (
            detected_fields[0].detection_source
            if detected_fields
            else DocumentField.DetectionSourceEnum.HEURISTIC
        )
        return Response(
            {
                **response_serializer.data,
                "page_count": page_count,
                "detection_summary": {
                    "source": detection_source,
                    "field_count": len(detected_fields),
                },
            },
            status=status.HTTP_201_CREATED,
        )


class AdminDocumentDetailView(APIView):
    def get(self, request, document_id):
        document = get_object_or_404(Document.objects.prefetch_related("fields", "signing_requests"), pk=document_id)
        serializer = AdminDocumentDetailSerializer(document)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request, document_id):
        document = get_object_or_404(Document, pk=document_id)
        serializer = DocumentUpdateSerializer(document, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AdminDocumentDetailSerializer(document).data, status=status.HTTP_200_OK)


class AdminDocumentFieldsView(APIView):
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
    def post(self, request, document_id):
        document = get_object_or_404(Document.objects.prefetch_related("fields", "signing_requests"), pk=document_id)
        serializer = DocumentSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if document.status != Document.StatusEnum.DRAFT:
            return Response(
                {"status": ["Only draft documents can be sent."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not document.fields.exists():
            return Response(
                {"fields": ["Add at least one field before sending."]},
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
            document.status = Document.StatusEnum.SENT
            document.save(update_fields=["status", "updated_at"])

        document = Document.objects.prefetch_related("fields", "signing_requests").get(pk=document.pk)
        response_serializer = AdminDocumentDetailSerializer(document)
        payload = response_serializer.data
        payload["status"] = document.status
        payload["signer_count"] = len(created_requests)
        return Response(payload, status=status.HTTP_200_OK)


class AdminDocumentVoidView(APIView):
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
