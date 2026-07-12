from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import fitz
from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.templatetags.static import static
from django.utils import timezone
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.documents.models import Document, DocumentField
from apps.documents.serializers import DocumentFieldSerializer
from services.pdf_engine import PDFEngine
from tasks.notifications import notify_admin_progress, send_completion_emails, send_otp_email
from utils.otp import generate_otp, hash_otp, verify_otp
from utils.signer_session import build_signer_session_token, verify_signer_session_token
from utils.task_dispatch import enqueue_task

from .models import FieldSubmission, SigningRequest


def mask_email(email: str) -> str:
    local_part, domain = email.split("@", 1)
    if len(local_part) <= 1:
        return f"{local_part[0]}***@{domain}"
    return f"{local_part[0]}***@{domain}"


def get_signing_request_or_404(token):
    return get_object_or_404(
        SigningRequest.objects.select_related("document").prefetch_related("document__fields"),
        pk=token,
    )


def sync_signing_request_status(signing_request: SigningRequest) -> None:
    if signing_request.status == SigningRequest.StatusEnum.SIGNED:
        return

    if signing_request.expires_at and signing_request.expires_at <= timezone.now():
        if signing_request.status != SigningRequest.StatusEnum.EXPIRED:
            signing_request.status = SigningRequest.StatusEnum.EXPIRED
            signing_request.save(update_fields=["status", "updated_at"])


def get_access_message(signing_request: SigningRequest) -> str | None:
    sync_signing_request_status(signing_request)
    if signing_request.document.status == Document.StatusEnum.VOIDED:
        return "This document has been voided and is no longer available for signing."
    if signing_request.status == SigningRequest.StatusEnum.SIGNED:
        return "This document has already been signed."
    if signing_request.status == SigningRequest.StatusEnum.EXPIRED:
        return "This signing link has expired."
    return None


def build_page_payload(signing_request: SigningRequest) -> list[dict[str, float | int | str]]:
    pages: list[dict[str, float | int | str]] = []
    with fitz.open(signing_request.document.original_pdf.path) as pdf_document:
        for page_number, page in enumerate(pdf_document, start=1):
            pages.append(
                {
                    "number": page_number,
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                    "preview_url": f"/api/sign/{signing_request.id}/pages/{page_number}/preview/",
                }
            )
    return pages


class SignerPortalView(TemplateView):
    template_name = "signing/portal.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["signing_token"] = str(kwargs["token"])
        context["signer_portal_css_url"] = static("signing/portal.css")
        context["signer_portal_js_url"] = static("signing/portal.js")
        return context


class SignerContextView(APIView):
    permission_classes = []
    authentication_classes = []

    def get(self, request, token):
        signing_request = get_signing_request_or_404(token)
        pages = build_page_payload(signing_request)
        payload = {
            "document_title": signing_request.document.title,
            "signer_name": signing_request.signer_name,
            "status": signing_request.status,
            "document_status": signing_request.document.status,
            "expires_at": signing_request.expires_at,
            "masked_email": mask_email(signing_request.signer_email),
            "access_message": get_access_message(signing_request),
            "page_count": len(pages),
            "pages": pages,
            "fields": DocumentFieldSerializer(signing_request.document.fields.all(), many=True).data,
        }
        return Response(payload, status=status.HTTP_200_OK)


class SignerPagePreviewView(APIView):
    permission_classes = []
    authentication_classes = []

    def get(self, request, token, page_number):
        signing_request = get_signing_request_or_404(token)
        with fitz.open(signing_request.document.original_pdf.path) as pdf_document:
            if page_number < 1 or page_number > pdf_document.page_count:
                raise Http404("Page not found.")
            page = pdf_document[page_number - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        return HttpResponse(pixmap.tobytes("png"), content_type="image/png")


class SignerOtpSendView(APIView):
    permission_classes = []
    authentication_classes = []

    def post(self, request, token):
        signing_request = get_signing_request_or_404(token)
        access_message = get_access_message(signing_request)
        if access_message:
            return Response({"detail": access_message}, status=status.HTTP_400_BAD_REQUEST)

        otp = getattr(settings, "SIGNACORE_TEST_OTP_CODE", None) or generate_otp()
        signing_request.otp_hash = hash_otp(otp)
        signing_request.otp_expires_at = timezone.now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)
        signing_request.save(update_fields=["otp_hash", "otp_expires_at", "updated_at"])
        enqueue_task(send_otp_email, str(signing_request.id), otp)
        return Response(
            {
                "masked_email": mask_email(signing_request.signer_email),
                "message": "OTP sent successfully.",
            },
            status=status.HTTP_200_OK,
        )


class SignerOtpVerifyView(APIView):
    permission_classes = []
    authentication_classes = []

    def post(self, request, token):
        signing_request = get_signing_request_or_404(token)
        access_message = get_access_message(signing_request)
        if access_message:
            return Response({"detail": access_message}, status=status.HTTP_400_BAD_REQUEST)

        otp = str(request.data.get("otp", "")).strip()
        if not otp or not signing_request.otp_hash or not signing_request.otp_expires_at:
            return Response({"otp": ["OTP has not been issued."]}, status=status.HTTP_400_BAD_REQUEST)
        if signing_request.otp_expires_at < timezone.now():
            return Response({"otp": ["OTP has expired."]}, status=status.HTTP_400_BAD_REQUEST)
        if not verify_otp(otp, signing_request.otp_hash):
            return Response({"otp": ["Invalid OTP."]}, status=status.HTTP_400_BAD_REQUEST)

        signing_request.status = SigningRequest.StatusEnum.OTP_VERIFIED
        signing_request.ip_address = request.META.get("REMOTE_ADDR", "")
        signing_request.user_agent = request.META.get("HTTP_USER_AGENT", "")
        signing_request.save(update_fields=["status", "ip_address", "user_agent", "updated_at"])
        return Response(
            {"session_token": build_signer_session_token(str(signing_request.id))},
            status=status.HTTP_200_OK,
        )


class SignerSubmitView(APIView):
    permission_classes = []
    authentication_classes = []
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, token):
        signing_request = get_signing_request_or_404(token)
        access_message = get_access_message(signing_request)
        if access_message:
            return Response({"detail": access_message}, status=status.HTTP_400_BAD_REQUEST)

        session_token = str(request.data.get("session_token", "")).strip()
        if not verify_signer_session_token(session_token, str(signing_request.id)):
            return Response({"session_token": ["Invalid or expired session token."]}, status=status.HTTP_400_BAD_REQUEST)

        field_errors: dict[str, list[str]] = {}
        submissions_to_create: list[FieldSubmission] = []

        for document_field in signing_request.document.fields.all():
            field_type_key = f"field_{document_field.id}_type"
            selected_type = request.data.get(field_type_key)
            if not selected_type:
                if document_field.is_required:
                    field_errors[str(document_field.id)] = ["This field is required."]
                continue

            expected_value_type = self._expected_value_type_for_field(document_field.field_type)
            if selected_type != expected_value_type:
                field_errors[str(document_field.id)] = ["Submitted value type does not match the field type."]
                continue

            if selected_type == FieldSubmission.ValueTypeEnum.TEXT:
                value = str(request.data.get(f"field_{document_field.id}_value", "")).strip()
                if not value and document_field.is_required:
                    field_errors[str(document_field.id)] = ["Text value is required."]
                    continue
                submissions_to_create.append(
                    FieldSubmission(
                        signing_request=signing_request,
                        document_field=document_field,
                        value_type=FieldSubmission.ValueTypeEnum.TEXT,
                        text_value=value,
                    )
                )
            elif selected_type == FieldSubmission.ValueTypeEnum.CHECKBOX:
                checked_value = str(request.data.get(f"field_{document_field.id}_checked", "")).strip().lower()
                is_checked = checked_value in {"1", "true", "yes", "on"}
                if document_field.is_required and not is_checked:
                    field_errors[str(document_field.id)] = ["Checkbox must be checked."]
                    continue
                submissions_to_create.append(
                    FieldSubmission(
                        signing_request=signing_request,
                        document_field=document_field,
                        value_type=FieldSubmission.ValueTypeEnum.CHECKBOX,
                        text_value="true" if is_checked else "false",
                    )
                )
            else:
                image = request.FILES.get(f"field_{document_field.id}_image")
                if image is None:
                    field_errors[str(document_field.id)] = ["Image value is required."]
                    continue
                submissions_to_create.append(
                    FieldSubmission(
                        signing_request=signing_request,
                        document_field=document_field,
                        value_type=selected_type,
                        image_value=image,
                    )
                )

        if field_errors:
            return Response({"field_errors": field_errors}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            signing_request.submissions.all().delete()
            for submission in submissions_to_create:
                submission.save()
            signing_request.status = SigningRequest.StatusEnum.SIGNED
            signing_request.signed_at = timezone.now()
            signing_request.ip_address = request.META.get("REMOTE_ADDR", "")
            signing_request.user_agent = request.META.get("HTTP_USER_AGENT", "")
            signing_request.save(update_fields=["status", "signed_at", "ip_address", "user_agent", "updated_at"])

            document = signing_request.document
            remaining = document.signing_requests.exclude(status=SigningRequest.StatusEnum.SIGNED).exists()
            if remaining:
                document.status = Document.StatusEnum.PARTIALLY_SIGNED
                document.save(update_fields=["status", "updated_at"])
                transaction.on_commit(
                    lambda document_id=str(document.id), signing_request_id=str(signing_request.id): enqueue_task(
                        notify_admin_progress,
                        document_id,
                        signing_request_id,
                    )
                )
                return Response(
                    {"status": Document.StatusEnum.PARTIALLY_SIGNED, "message": "Signature submitted."},
                    status=status.HTTP_200_OK,
                )

            all_submissions = (
                FieldSubmission.objects.select_related("document_field")
                .filter(signing_request__document=document)
                .order_by("submitted_at")
            )
            output_relative_path = f"signacore/signed/{document.id}_signed.pdf"
            output_path = Path(settings.MEDIA_ROOT) / output_relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            PDFEngine().flatten(
                document.original_pdf.path,
                output_path,
                [
                    {
                        "page": submission.document_field.page,
                        "x": submission.document_field.x,
                        "y": submission.document_field.y,
                        "width": submission.document_field.width,
                        "height": submission.document_field.height,
                        "value_type": submission.value_type,
                        "text_value": submission.text_value,
                        "image_path": submission.image_value.path if submission.image_value else "",
                    }
                    for submission in all_submissions
                ],
            )
            document.status = Document.StatusEnum.COMPLETED
            document.signed_pdf.name = output_relative_path
            document.save(update_fields=["status", "signed_pdf", "updated_at"])
            transaction.on_commit(
                lambda document_id=str(document.id): enqueue_task(
                    send_completion_emails,
                    document_id,
                )
            )

        return Response(
            {"status": Document.StatusEnum.COMPLETED, "message": "Document signed successfully."},
            status=status.HTTP_200_OK,
        )

    def _expected_value_type_for_field(self, field_type: str) -> str:
        if field_type == DocumentField.FieldTypeEnum.SIGNATURE:
            return FieldSubmission.ValueTypeEnum.SIGNATURE_PNG
        if field_type == DocumentField.FieldTypeEnum.INITIALS:
            return FieldSubmission.ValueTypeEnum.INITIALS_PNG
        if field_type == DocumentField.FieldTypeEnum.CHECKBOX:
            return FieldSubmission.ValueTypeEnum.CHECKBOX
        return FieldSubmission.ValueTypeEnum.TEXT
