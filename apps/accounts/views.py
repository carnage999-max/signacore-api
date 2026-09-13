from __future__ import annotations

import hashlib
import hmac

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.tokens import default_token_generator
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.documents.auth import HasValidSignacoreSecret
from apps.documents.models import AdminAuditLog
from apps.documents.views import get_request_ip
from apps.signing.models import SigningRequest
from services.oauth_client import OAuthExchangeError, exchange_oauth_code
from tasks.notifications import (
    send_account_login_alert,
    send_account_verification,
    send_account_welcome,
)
from utils.identity import email_digest
from utils.task_dispatch import enqueue_task

from .models import AccountProfile, OAuthIntentEnum, Organization, OrganizationMembership, SocialIdentity
from .serializers import (
    AccountSessionSerializer,
    AccountSigningRequestSerializer,
    EmailLoginSerializer,
    EmailRegistrationSerializer,
    EmailVerificationSerializer,
    OAuthExchangeSerializer,
)


DUMMY_PASSWORD_HASH = make_password(None)


def subject_digest(provider: str, subject: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(),
        f"{provider}:{subject}".encode(),
        hashlib.sha256,
    ).hexdigest()


def build_account_payload(user, *, is_new: bool) -> dict:
    profile = user.signacore_profile
    memberships = user.signacore_memberships.select_related("organization").filter(
        status=OrganizationMembership.StatusEnum.ACTIVE,
        organization__status=Organization.StatusEnum.ACTIVE,
    )
    organizations = [
        {
            "id": str(membership.organization_id),
            "name": membership.organization.name,
            "role": membership.role,
        }
        for membership in memberships
    ]
    display_name = profile.display_name or user.username
    return {
        "id": user.id,
        "username": user.username,
        "email": profile.email,
        "display_name": display_name,
        "first_name": "",
        "last_name": "",
        "full_name": display_name,
        "account_type": profile.account_type,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "is_new": is_new,
        "organizations": organizations,
    }


def link_signing_requests(user, email: str) -> None:
    SigningRequest.objects.filter(
        signer_email_hash=email_digest(email),
        signer_user__isnull=True,
    ).update(signer_user=user)


def log_account_event(request, user, action: str, summary: str) -> None:
    organization = (
        user.signacore_memberships.select_related("organization")
        .filter(status=OrganizationMembership.StatusEnum.ACTIVE)
        .values_list("organization", flat=True)
        .first()
    )
    AdminAuditLog.objects.create(
        actor=user,
        organization_id=organization,
        actor_email=user.signacore_profile.email,
        action=action,
        target_type="account",
        target_id=str(user.id),
        summary=summary,
        ip_address=get_request_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )


class EmailRegistrationView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "email_auth"
    serializer_class = EmailRegistrationSerializer

    def post(self, request):
        serializer = EmailRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        email = values["email"]
        digest = email_digest(email)
        existing_profile = (
            AccountProfile.objects.select_related("user")
            .filter(email_hash=digest)
            .first()
        )
        if existing_profile is not None:
            existing_user = existing_profile.user
            if (
                not existing_user.is_active
                and existing_profile.account_type == values["account_type"]
                and existing_user.check_password(values["password"])
            ):
                enqueue_task(send_account_verification, existing_user.id)
                return Response(
                    {"detail": "Check your email to verify your account."},
                    status=status.HTTP_200_OK,
                )
            return Response(
                {"detail": "An account already uses this email address."},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            with transaction.atomic():
                account_type = values["account_type"]
                user = get_user_model().objects.create_user(
                    username=f"email_{digest[:24]}",
                    email="",
                    password=values["password"],
                    is_active=False,
                    is_staff=account_type == AccountProfile.AccountTypeEnum.COMPANY,
                )
                AccountProfile.objects.create(
                    user=user,
                    account_type=account_type,
                    email=email,
                    display_name=values["display_name"].strip(),
                )
                if account_type == AccountProfile.AccountTypeEnum.COMPANY:
                    organization = Organization.objects.create(
                        name=values["company_name"].strip(),
                        created_by=user,
                    )
                    OrganizationMembership.objects.create(
                        organization=organization,
                        user=user,
                        role=OrganizationMembership.RoleEnum.OWNER,
                    )
                link_signing_requests(user, email)
                log_account_event(
                    request,
                    user,
                    AdminAuditLog.ActionEnum.EMAIL_REGISTER,
                    "Created an account with email and password.",
                )
        except IntegrityError:
            return Response(
                {"detail": "An account already uses this email address."},
                status=status.HTTP_409_CONFLICT,
            )

        enqueue_task(send_account_verification, user.id)
        return Response(
            {"detail": "Check your email to verify your account."},
            status=status.HTTP_201_CREATED,
        )


class EmailVerificationView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "email_auth"
    serializer_class = EmailVerificationSerializer

    def post(self, request):
        serializer = EmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user_id = urlsafe_base64_decode(serializer.validated_data["uid"]).decode()
        except (ValueError, TypeError, OverflowError, UnicodeDecodeError):
            user_id = ""
        with transaction.atomic():
            user = (
                get_user_model()
                .objects.select_for_update()
                .select_related("signacore_profile")
                .filter(pk=user_id)
                .first()
            )
            if user is None or not default_token_generator.check_token(
                user,
                serializer.validated_data["token"],
            ):
                return Response(
                    {"detail": "This verification link is invalid or expired."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            was_inactive = not user.is_active
            if was_inactive:
                user.is_active = True
                user.save(update_fields=["is_active"])
                log_account_event(
                    request,
                    user,
                    AdminAuditLog.ActionEnum.EMAIL_LOGIN,
                    "Verified email and signed in.",
                )
        payload = build_account_payload(user, is_new=True)
        if was_inactive:
            enqueue_task(send_account_welcome, user.id)
        return Response(AccountSessionSerializer(payload).data, status=status.HTTP_200_OK)


class EmailLoginView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "email_auth"
    serializer_class = EmailLoginSerializer

    def post(self, request):
        serializer = EmailLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        profile = (
            AccountProfile.objects.select_related("user")
            .filter(email_hash=email_digest(values["email"]))
            .first()
        )
        encoded_password = profile.user.password if profile is not None else DUMMY_PASSWORD_HASH
        password_matches = check_password(values["password"], encoded_password)
        if (
            profile is None
            or not password_matches
            or not profile.user.is_active
            or profile.account_type != values["account_type"]
        ):
            return Response(
                {"detail": "The email or password is incorrect."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user = profile.user
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        log_account_event(
            request,
            user,
            AdminAuditLog.ActionEnum.EMAIL_LOGIN,
            "Signed in with email and password.",
        )
        payload = build_account_payload(user, is_new=False)
        enqueue_task(send_account_login_alert, user.id, "email and password")
        return Response(AccountSessionSerializer(payload).data, status=status.HTTP_200_OK)


class OAuthExchangeView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = OAuthExchangeSerializer

    def post(self, request):
        serializer = OAuthExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            verified_identity = exchange_oauth_code(
                provider=values["provider"],
                code=values["code"],
                redirect_uri=values["redirect_uri"],
                nonce=values["nonce"],
            )
        except (OAuthExchangeError, httpx.HTTPError):
            return Response(
                {"detail": "The identity provider could not verify this sign-in."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        digest = subject_digest(verified_identity.provider, verified_identity.subject)
        identity = SocialIdentity.objects.select_related("user__signacore_profile").filter(
            provider=verified_identity.provider,
            subject_hash=digest,
        ).first()
        is_new = identity is None
        linked_profile = None

        if is_new and values["intent"] == OAuthIntentEnum.LOGIN:
            return Response(
                {"detail": "No SignaCore account is connected to this provider."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if is_new:
            linked_profile = (
                AccountProfile.objects.select_related("user")
                .filter(email_hash=email_digest(verified_identity.email))
                .first()
            )
            if linked_profile is not None and (
                linked_profile.account_type != values["account_type"]
                or not linked_profile.user.is_active
            ):
                return Response(
                    {"detail": "This email is already connected to another SignaCore account."},
                    status=status.HTTP_409_CONFLICT,
                )
            if linked_profile is not None:
                is_new = False

        if (
            is_new
            and values["account_type"] == AccountProfile.AccountTypeEnum.COMPANY
            and not values.get("company_name", "").strip()
        ):
            return Response(
                {"company_name": ["Company name is required for a new company account."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if identity is None and linked_profile is not None:
                user = linked_profile.user
                identity = SocialIdentity.objects.create(
                    user=user,
                    provider=verified_identity.provider,
                    subject_hash=digest,
                    subject=verified_identity.subject,
                    email=verified_identity.email,
                )
                linked_profile.email = verified_identity.email
                if verified_identity.display_name:
                    linked_profile.display_name = verified_identity.display_name
                linked_profile.save(update_fields=["email", "display_name", "updated_at"])
            elif identity is None:
                account_type = values["account_type"]
                username = f"oauth_{verified_identity.provider.lower()}_{digest[:24]}"
                user = get_user_model().objects.create_user(
                    username=username,
                    email="",
                    is_active=True,
                    is_staff=account_type == AccountProfile.AccountTypeEnum.COMPANY,
                )
                user.set_unusable_password()
                user.save(update_fields=["password"])
                display_name = (
                    verified_identity.display_name
                    or values.get("display_name", "").strip()
                    or verified_identity.email.split("@", 1)[0]
                )
                AccountProfile.objects.create(
                    user=user,
                    account_type=account_type,
                    email=verified_identity.email,
                    display_name=display_name,
                )
                identity = SocialIdentity.objects.create(
                    user=user,
                    provider=verified_identity.provider,
                    subject_hash=digest,
                    subject=verified_identity.subject,
                    email=verified_identity.email,
                )
                if account_type == AccountProfile.AccountTypeEnum.COMPANY:
                    organization = Organization.objects.create(
                        name=values["company_name"].strip(),
                        created_by=user,
                    )
                    OrganizationMembership.objects.create(
                        organization=organization,
                        user=user,
                        role=OrganizationMembership.RoleEnum.OWNER,
                    )
                    AdminAuditLog.objects.create(
                        actor=user,
                        organization=organization,
                        actor_email=verified_identity.email,
                        action=AdminAuditLog.ActionEnum.OAUTH_LOGIN,
                        target_type="account",
                        target_id=str(user.id),
                        summary=f"Created a company account with {identity.get_provider_display()}.",
                        ip_address=get_request_ip(request),
                        user_agent=request.META.get("HTTP_USER_AGENT", ""),
                    )
            else:
                user = identity.user
                if not user.is_active:
                    return Response(
                        {"detail": "This SignaCore account is not active."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                identity.email = verified_identity.email
                identity.save(update_fields=["email", "last_login_at"])
                profile = user.signacore_profile
                profile.email = verified_identity.email
                if verified_identity.display_name:
                    profile.display_name = verified_identity.display_name
                profile.save(update_fields=["email", "display_name", "updated_at"])

            link_signing_requests(user, verified_identity.email)

        payload = build_account_payload(user, is_new=is_new)
        if is_new:
            enqueue_task(send_account_welcome, user.id)
        else:
            enqueue_task(
                send_account_login_alert,
                user.id,
                identity.get_provider_display(),
            )
        return Response(AccountSessionSerializer(payload).data, status=status.HTTP_200_OK)


class AccountSigningRequestsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = AccountSigningRequestSerializer

    def get(self, request):
        account_id = request.headers.get("X-Signacore-Account-Id", "").strip()
        user = get_user_model().objects.filter(pk=account_id, is_active=True).first()
        if not user or not hasattr(user, "signacore_profile"):
            return Response({"detail": "Account access is required."}, status=status.HTTP_403_FORBIDDEN)
        requests = SigningRequest.objects.select_related("document__organization").filter(
            signer_user=user,
        )[:100]
        payload = [
            {
                "id": signing_request.id,
                "document_title": signing_request.document.title,
                "company_name": signing_request.document.organization.name,
                "status": signing_request.status,
                "expires_at": signing_request.expires_at,
                "signed_at": signing_request.signed_at,
            }
            for signing_request in requests
        ]
        return Response({"items": AccountSigningRequestSerializer(payload, many=True).data})
