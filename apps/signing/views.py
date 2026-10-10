from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import fitz
from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.documents.models import Document, DocumentField
from apps.documents.serializers import DocumentFieldSerializer
from services.document_completion import issue_signed_copy, refresh_document_status
from services.pdf_sanitizer import PDFSanitizationError
from tasks.notifications import notify_admin_progress, send_otp_email, send_signed_copy
from utils.file_storage import temporary_plaintext_file
from utils.otp import generate_otp, hash_otp, verify_otp
from utils.pdf_preview import (
    build_preview_matrix,
    prepare_page_for_preview,
    preview_etag,
    preview_is_unchanged,
    with_preview_caching,
)
from utils.request_meta import get_client_ip, get_user_agent
from utils.signer_session import build_signer_session_token, verify_signer_session_token
from utils.static_assets import versioned_static
from utils.task_dispatch import enqueue_task
from utils.throttling import SignacoreRateThrottle

from .events import record_signing_event
from .models import FieldSubmission, SigningEvent, SigningRequest
from .serializers import FieldSubmissionSerializer, SignerOtpSerializer, SigningRequestSerializer

logger = logging.getLogger(__name__)

SIGNER_SESSION_COOKIE = "signacore_signer_session"
SIGNER_SESSION_MAX_AGE_SECONDS = 3600


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
        context["signer_portal_css_url"] = versioned_static("signing/portal.css")
        context["signer_portal_js_url"] = versioned_static("signing/portal.js")
        # Versioned like the rest: a browser holds on to a favicon harder than anything else
        # it caches, so replacing one without a new URL leaves the old mark on the tab.
        context["signer_portal_icon_url"] = versioned_static("signing/favicon.ico")
        return context


class SignerPortalLandingView(TemplateView):
    """What the signing host says to somebody who arrives without a link.

    The host answered a raw nginx 404 at its root and a bare Django 404 at /sign/, which is a
    broken-looking door on the one domain a signer is told to trust. Most people who land here have
    a link their mail client truncated, or typed the domain from memory, so the page is the answer
    to that: where the email is, what to do when a link or a code has expired, and who to ask.

    The numbers come from the settings they describe rather than being written into the copy, so
    the page cannot quietly disagree with what the service actually does.
    """

    template_name = "signing/portal_landing.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["signer_portal_css_url"] = versioned_static("signing/portal.css")
        context["signer_portal_icon_url"] = versioned_static("signing/favicon.ico")
        context["link_expiry_days"] = settings.SIGNING_LINK_EXPIRY_DAYS
        context["otp_expiry_minutes"] = settings.OTP_EXPIRY_MINUTES
        context["otp_cooldown_seconds"] = settings.OTP_RESEND_COOLDOWN_SECONDS
        context["help_url"] = f"{settings.SIGNACORE_APP_URL.rstrip('/')}/help"
        context["support_email"] = "info@mysignacore.com"
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
        record_signing_event(signing_request, SigningEvent.EventEnum.OPENED, request=request)
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
            # Whether this signer has finished, and whether the completed copy exists to be kept.
            # A document is not editable once signed, and the copy can lag the signature when the
            # packaging had to be retried.
            "has_signed": signing_request.status == SigningRequest.StatusEnum.SIGNED,
            # This signer's copy, which does not depend on anybody else having signed.
            "signed_copy_ready": bool(signing_request.signed_pdf),
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

        # After the session check, never before it: a reader who may not see this page must not
        # learn from a 304 that it exists and is unchanged.
        requested_width = request.query_params.get("width")
        etag = preview_etag(signing_request.document, page_number, requested_width)
        if preview_is_unchanged(request, etag):
            return with_preview_caching(HttpResponseNotModified(), etag)

        with temporary_plaintext_file(signing_request.document.original_pdf, suffix=".pdf") as pdf_path:
            with fitz.open(pdf_path) as pdf_document:
                if page_number < 1 or page_number > pdf_document.page_count:
                    raise Http404("Page not found.")
                page = pdf_document[page_number - 1]
                prepare_page_for_preview(page)
                matrix = build_preview_matrix(page, requested_width)
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return with_preview_caching(HttpResponse(pixmap.tobytes("png"), content_type="image/png"), etag)


class SignerSignedCopyView(APIView):
    """Serve the completed document to a signer who signed it.

    The same verified session that let them fill the document lets them keep a copy of what they
    put their name to, which otherwise exists only in an email they may never have received.
    """

    permission_classes = []
    authentication_classes = []
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "signer_preview"
    serializer_class = SigningRequestSerializer
    # A file, or a short reason why not. Never a web page: the browsable renderer answers a
    # navigation with a full HTML document, and this endpoint used to be the target of an anchor
    # carrying the download attribute, so that page was saved to disk under the document's name.
    renderer_classes = [JSONRenderer]

    def perform_content_negotiation(self, request, force=False):
        """Answer in the one form this endpoint has, rather than refusing the request.

        Restricting the renderer would otherwise make any client that does not ask for JSON -
        including a browser, which asks for HTML - fail negotiation with 406 before the handler
        runs, and that would take the file with it.
        """
        return super().perform_content_negotiation(request, force=True)

    def get(self, request, token):
        signing_request = get_signing_request_or_404(token)
        if not has_verified_signer_session(request, signing_request):
            return Response(
                {"detail": "Verify your email before downloading this document."},
                status=status.HTTP_403_FORBIDDEN,
            )

        document = signing_request.document
        if not signing_request.signed_pdf:
            return Response(
                {"detail": "Your copy is not ready yet. It will be emailed to you as soon as it is."},
                status=status.HTTP_404_NOT_FOUND,
            )

        with temporary_plaintext_file(signing_request.signed_pdf, suffix=".pdf") as signed_path:
            payload = Path(signed_path).read_bytes()

        response = HttpResponse(payload, content_type="application/pdf")
        filename = f"{slugify(document.title) or 'document'}-signed.pdf"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class SignerSenderSignatureView(APIView):
    """The mark the other party already made, shown to the person being asked to sign.

    Somebody agreeing to a countersigned contract should be able to see that it is countersigned
    before they add their own name to it, not only afterwards on the copy that arrives by email.

    Gated on the same verified session as the document itself, and only ever for a field the
    sender owns: nothing here can reach a signature belonging to another signer.
    """

    permission_classes = []
    authentication_classes = []
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "signer_preview"
    # This returns a PNG rather than a serialized object, but the schema generator cannot know
    # that and warns on every build without one. The page preview beside it says the same thing
    # for the same reason.
    serializer_class = SigningRequestSerializer

    def get(self, request, token, field_id):
        signing_request = get_signing_request_or_404(token)
        if not has_verified_signer_session(request, signing_request):
            return Response({"detail": "Verification required."}, status=status.HTTP_403_FORBIDDEN)

        field = get_object_or_404(
            DocumentField,
            pk=field_id,
            document=signing_request.document,
            assigned_to=DocumentField.AssignedToEnum.SENDER,
        )
        if not field.sender_signature:
            raise Http404

        with temporary_plaintext_file(field.sender_signature, suffix=".png") as path:
            payload = Path(path).read_bytes()
        response = HttpResponse(payload, content_type="image/png")
        # Private: it is one party's signature on one agreement, not a public asset.
        response["Cache-Control"] = "private, max-age=300"
        return response


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
        record_signing_event(
            signing_request,
            SigningEvent.EventEnum.CODE_SENT,
            request=request,
            detail=f"Code sent to {mask_email(signing_request.signer_email)}",
        )
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
        signing_request.otp_verified_at = timezone.now()
        signing_request.ip_address = get_client_ip(request)
        signing_request.user_agent = get_user_agent(request)
        signing_request.save(update_fields=["status", "otp_verified_at", "ip_address", "user_agent", "updated_at"])
        record_signing_event(
            signing_request,
            SigningEvent.EventEnum.CODE_VERIFIED,
            request=request,
            detail=f"Code confirmed at {mask_email(signing_request.signer_email)}",
        )
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

            # The sender's field. Never the signer's to complete, and not theirs to change
            # either: a value they could overwrite is not a value the sender can rely on having
            # sent. Keyed on whose field it is rather than on whether it has a value, or a
            # sender who left their own field blank would hand it to the signer by accident.
            # Refused rather than ignored, so a client that tries is told.
            if document_field.assigned_to == DocumentField.AssignedToEnum.SENDER:
                if selected_type:
                    field_errors[str(document_field.id)] = [
                        "This field was completed by the sender and cannot be changed.",
                    ]
                continue

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
            signing_request.user_agent = get_user_agent(request)
            signing_request.save(update_fields=["status", "signed_at", "ip_address", "user_agent", "updated_at"])
            field_count = len(submissions_to_create)
            record_signing_event(
                signing_request,
                SigningEvent.EventEnum.SIGNED,
                request=request,
                detail=f"{field_count} assigned {'field' if field_count == 1 else 'fields'} completed",
            )

            document = signing_request.document

            try:
                # Made from this signer's own answers, so it is ready the moment they finish and
                # does not wait on people they have never met.
                issue_signed_copy(signing_request)
            except PDFSanitizationError:
                # The signature is kept; only the unsafe file is withheld. The reconciler tries
                # again, which is what recovers a copy whose failure was later fixed.
                logger.exception(
                    "Signed copy failed sanitization",
                    extra={
                        "document_id": str(document.id),
                        "signing_request_id": str(signing_request.id),
                    },
                )
                transaction.on_commit(
                    lambda document_id=str(document.id), request_id=str(signing_request.id): enqueue_task(
                        notify_admin_progress, document_id, request_id
                    )
                )
                return Response(
                    {
                        "status": document.status,
                        "message": (
                            "Your signature was recorded. Your copy needs attention from the "
                            "sender before it can be issued."
                        ),
                    },
                    status=status.HTTP_202_ACCEPTED,
                )

            refresh_document_status(document)

            # The signer gets their copy and the sender is told, both now rather than when the
            # last of the other signers happens to finish.
            transaction.on_commit(lambda request_id=str(signing_request.id): enqueue_task(send_signed_copy, request_id))
            transaction.on_commit(
                lambda document_id=str(document.id), request_id=str(signing_request.id): enqueue_task(
                    notify_admin_progress, document_id, request_id
                )
            )

        return Response(
            {
                "status": document.status,
                "message": "Document signed successfully. Your copy is on its way by email.",
            },
            status=status.HTTP_200_OK,
        )

    def _expected_value_type_for_field(self, field_type: str) -> str:
        if field_type == DocumentField.FieldTypeEnum.SIGNATURE:
            return FieldSubmission.ValueTypeEnum.SIGNATURE_PNG
        if field_type == DocumentField.FieldTypeEnum.INITIALS:
            return FieldSubmission.ValueTypeEnum.INITIALS_PNG
        if field_type in (DocumentField.FieldTypeEnum.CHECKBOX, DocumentField.FieldTypeEnum.RADIO):
            # A radio option records whether it is the one chosen, exactly as a checkbox does.
            return FieldSubmission.ValueTypeEnum.CHECKBOX
        return FieldSubmission.ValueTypeEnum.TEXT
