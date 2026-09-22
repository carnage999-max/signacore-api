from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta

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
from utils.file_storage import save_encrypted_field_file, temporary_output_file, temporary_plaintext_file
from utils.otp import generate_otp, hash_otp, verify_otp
from utils.signer_session import build_signer_session_token, verify_signer_session_token
from utils.task_dispatch import enqueue_task
from utils.throttling import SignacoreRateThrottle

from .models import FieldSubmission, SigningRequest
from .serializers import FieldSubmissionSerializer, SignerOtpSerializer, SigningRequestSerializer

SIGNER_SESSION_COOKIE = "signacore_signer_session"
SIGNER_SESSION_MAX_AGE_SECONDS = 3600


def mask_email(email: str) -> str:
    local_part, domain = email.split("@", 1)
    if len(local_part) <= 1:
        return f"{local_part[0]}***@{domain}"
    return f"{local_part[0]}***@{domain}"


def get_client_ip(request) -> str:
    real_ip = str(request.META.get("HTTP_X_REAL_IP", "")).strip()
    if real_ip:
        return real_ip

    forwarded_for = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return str(request.META.get("REMOTE_ADDR", "")).strip()


def get_signing_request_or_404(token):
    return get_object_or_404(
        SigningRequest.objects.select_related("document").prefetch_related("document__fields"),
        pk=token,
    )


def get_request_session_token(request) -> str:
    return str(request.COOKIES.get(SIGNER_SESSION_COOKIE, "") or "").strip()


def has_verified_signer_session(request, signing_request: SigningRequest) -> bool:
    return verify_signer_session_token(
        get_request_session_token(request),
        str(signing_request.id),
        signing_request.otp_hash,
        max_age_seconds=SIGNER_SESSION_MAX_AGE_SECONDS,
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
    with temporary_plaintext_file(signing_request.document.original_pdf, suffix=".pdf") as pdf_path:
        with fitz.open(pdf_path) as pdf_document:
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
        context["signer_account_url"] = f"{settings.SIGNACORE_APP_URL.rstrip('/')}/register?role=signer"
        return context


class SignerContextView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "signer_context"
    serializer_class = SigningRequestSerializer

    def get(self, request, token):
        signing_request = get_signing_request_or_404(token)
        access_message = get_access_message(signing_request)
        if not has_verified_signer_session(request, signing_request):
            return Response(
                {
                    "is_verified": False,
                    "document_title": "Document verification required",
                    "signer_name": "Signer",
                    "status": signing_request.status,
                    "document_status": signing_request.document.status,
                    "expires_at": signing_request.expires_at,
                    "masked_email": "",
                    "access_message": access_message,
                    "page_count": 0,
                    "pages": [],
                    "fields": [],
                },
                status=status.HTTP_200_OK,
            )

        pages = build_page_payload(signing_request)
        payload = {
            "is_verified": True,
            "document_title": signing_request.document.title,
            "signer_name": signing_request.signer_name,
            "status": signing_request.status,
            "document_status": signing_request.document.status,
            "expires_at": signing_request.expires_at,
            "masked_email": mask_email(signing_request.signer_email),
            "access_message": access_message,
            "page_count": len(pages),
            "pages": pages,
            "fields": DocumentFieldSerializer(signing_request.document.fields.all(), many=True).data,
        }
        return Response(payload, status=status.HTTP_200_OK)


class SignerPagePreviewView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "signer_preview"
    serializer_class = SigningRequestSerializer

    def get(self, request, token, page_number):
        signing_request = get_signing_request_or_404(token)
        if not has_verified_signer_session(request, signing_request):
            return Response(
                {"detail": "Verify your email before viewing this document."},
                status=status.HTTP_403_FORBIDDEN,
            )
        with temporary_plaintext_file(signing_request.document.original_pdf, suffix=".pdf") as pdf_path:
            with fitz.open(pdf_path) as pdf_document:
                if page_number < 1 or page_number > pdf_document.page_count:
                    raise Http404("Page not found.")
                page = pdf_document[page_number - 1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        return HttpResponse(pixmap.tobytes("png"), content_type="image/png")


class SignerOtpSendView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "signer_otp_send"
    serializer_class = SigningRequestSerializer

    def post(self, request, token):
        signing_request = get_signing_request_or_404(token)
        access_message = get_access_message(signing_request)
        if access_message:
            return Response({"detail": access_message}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        cooldown_seconds = int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60))
        if signing_request.otp_last_sent_at and cooldown_seconds > 0:
            elapsed = (now - signing_request.otp_last_sent_at).total_seconds()
            if elapsed < cooldown_seconds:
                retry_after = max(1, int(cooldown_seconds - elapsed))
                return Response(
                    {
                        "detail": "Please wait before requesting another verification code.",
                        "retry_after": retry_after,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        otp = getattr(settings, "SIGNACORE_TEST_OTP_CODE", None) or generate_otp()
        signing_request.otp_hash = hash_otp(otp)
        signing_request.otp_expires_at = now + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)
        signing_request.otp_last_sent_at = now
        signing_request.save(update_fields=["otp_hash", "otp_expires_at", "otp_last_sent_at", "updated_at"])
        enqueue_task(send_otp_email, str(signing_request.id), otp)
        return Response(
            {
                "masked_email": mask_email(signing_request.signer_email),
                "message": "Verification code sent successfully.",
                "retry_after": cooldown_seconds,
            },
            status=status.HTTP_200_OK,
        )


class SignerOtpVerifyView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "signer_otp_verify"
    serializer_class = SignerOtpSerializer

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
        signing_request.ip_address = get_client_ip(request)
        signing_request.user_agent = request.META.get("HTTP_USER_AGENT", "")
        signing_request.save(update_fields=["status", "ip_address", "user_agent", "updated_at"])
        session_token = build_signer_session_token(str(signing_request.id), signing_request.otp_hash)
        response = Response(
            {"session_token": session_token},
            status=status.HTTP_200_OK,
        )
        response.set_cookie(
            SIGNER_SESSION_COOKIE,
            session_token,
            max_age=SIGNER_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=request.is_secure(),
            samesite="Strict",
            path=f"/api/sign/{signing_request.id}/",
        )
        return response


class SignerSubmitView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "signer_submit"
    parser_classes = [MultiPartParser, FormParser]
    serializer_class = FieldSubmissionSerializer

    def post(self, request, token):
        signing_request = get_signing_request_or_404(token)
        access_message = get_access_message(signing_request)
        if access_message:
            return Response({"detail": access_message}, status=status.HTTP_400_BAD_REQUEST)

        session_token = str(request.data.get("session_token", "")).strip() or get_request_session_token(request)
        if not verify_signer_session_token(session_token, str(signing_request.id), signing_request.otp_hash):
            return Response(
                {"session_token": ["Invalid or expired session token."]}, status=status.HTTP_400_BAD_REQUEST
            )

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
            signing_request.ip_address = get_client_ip(request)
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

            all_submissions = list(
                FieldSubmission.objects.select_related("document_field")
                .filter(signing_request__document=document)
                .order_by("submitted_at")
            )
            with ExitStack() as stack:
                source_path = stack.enter_context(temporary_plaintext_file(document.original_pdf, suffix=".pdf"))
                output_path = stack.enter_context(temporary_output_file(suffix=".pdf"))
                flatten_submissions = []
                for submission in all_submissions:
                    image_path = ""
                    if submission.image_value:
                        image_path = str(
                            stack.enter_context(temporary_plaintext_file(submission.image_value, suffix=".png"))
                        )
                    flatten_submissions.append(
                        {
                            "page": submission.document_field.page,
                            "field_type": submission.document_field.field_type,
                            "x": submission.document_field.x,
                            "y": submission.document_field.y,
                            "width": submission.document_field.width,
                            "height": submission.document_field.height,
                            "value_type": submission.value_type,
                            "text_value": submission.text_value,
                            "image_path": image_path,
                        }
                    )
                PDFEngine().flatten(source_path, output_path, flatten_submissions)
                save_encrypted_field_file(
                    document.signed_pdf,
                    output_path,
                    filename=f"{document.id}-signed.pdf",
                )
            document.status = Document.StatusEnum.COMPLETED
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
