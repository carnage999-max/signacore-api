from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.tokens import default_token_generator
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.entitlements import PlanFeatureEnum, require_feature
from apps.documents.auth import HasValidSignacoreSecret, get_actor_organization, get_admin_actor
from apps.documents.models import AdminAuditLog
from apps.documents.views import get_request_ip
from apps.signing.models import SigningRequest
from services.oauth_client import OAuthExchangeError, exchange_oauth_code
from tasks.notifications import (
    send_account_login_alert,
    send_account_verification,
    send_account_welcome,
    send_organization_invitation,
    sync_organization_seat_quantity,
)
from utils.identity import email_digest
from utils.task_dispatch import enqueue_task
from utils.throttling import SignacoreRateThrottle

from .models import (
    AccountProfile,
    OAuthIntentEnum,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    SocialIdentity,
)
from .serializers import (
    AccountSessionSerializer,
    AccountSigningRequestSerializer,
    EmailLoginSerializer,
    EmailRegistrationSerializer,
    EmailVerificationSerializer,
    OAuthExchangeSerializer,
    OrganizationInvitationSerializer,
    OrganizationMemberSerializer,
)

DUMMY_PASSWORD_HASH = make_password(None)
logger = logging.getLogger(__name__)


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


def invitation_token_digest(token: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), token.encode(), hashlib.sha256).hexdigest()


def get_active_invitation(token: str) -> OrganizationInvitation | None:
    if not token:
        return None
    invitation = (
        OrganizationInvitation.objects.select_related("organization")
        .filter(token_hash=invitation_token_digest(token), accepted_at__isnull=True)
        .first()
    )
    if invitation is None or invitation.expires_at <= timezone.now():
        return None
    return invitation


def serialize_organization_members(organization: Organization) -> list[dict]:
    members = []
    for membership in organization.memberships.select_related("user", "user__signacore_profile"):
        profile = getattr(membership.user, "signacore_profile", None)
        members.append(
            {
                "id": membership.id,
                "user_id": membership.user_id,
                "name": (profile.display_name if profile else "") or membership.user.get_username(),
                "email": profile.email if profile else membership.user.email,
                "role": membership.role,
                "status": membership.status,
                "joined_at": membership.created_at,
            }
        )
    for invitation in organization.invitations.filter(accepted_at__isnull=True, expires_at__gt=timezone.now()):
        members.append(
            {
                "id": invitation.id,
                "user_id": None,
                "name": "Pending invitation",
                "email": invitation.email,
                "role": invitation.role,
                "status": OrganizationMembership.StatusEnum.INVITED,
                "joined_at": invitation.created_at,
            }
        )
    return OrganizationMemberSerializer(members, many=True).data


class EmailRegistrationView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "email_auth"
    serializer_class = EmailRegistrationSerializer

    def post(self, request):
        serializer = EmailRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        email = values["email"]
        digest = email_digest(email)
        invitation_token = values.get("invitation_token", "")
        invitation = get_active_invitation(invitation_token)
        if invitation_token and invitation is None:
            return Response(
                {"detail": "This workspace invitation is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if invitation is not None and values["account_type"] != AccountProfile.AccountTypeEnum.COMPANY:
            return Response(
                {"detail": "This invitation is for a company workspace account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        existing_profile = AccountProfile.objects.select_related("user").filter(email_hash=digest).first()
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
                if invitation is not None:
                    OrganizationMembership.objects.create(
                        organization=invitation.organization,
                        user=user,
                        role=invitation.role,
                    )
                    invitation.accepted_at = timezone.now()
                    invitation.save(update_fields=["accepted_at"])
                    enqueue_task(sync_organization_seat_quantity, str(invitation.organization_id))
                elif account_type == AccountProfile.AccountTypeEnum.COMPANY:
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
                    (
                        "Joined a company workspace with email and password."
                        if invitation is not None
                        else "Created an account with email and password."
                    ),
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
    throttle_classes = [SignacoreRateThrottle]
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
                for organization_id in user.signacore_memberships.filter(
                    status=OrganizationMembership.StatusEnum.ACTIVE,
                ).values_list("organization_id", flat=True):
                    enqueue_task(sync_organization_seat_quantity, str(organization_id))
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
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "email_auth"
    serializer_class = EmailLoginSerializer

    def post(self, request):
        serializer = EmailLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        profile = AccountProfile.objects.select_related("user").filter(email_hash=email_digest(values["email"])).first()
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
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "oauth_exchange"
    serializer_class = OAuthExchangeSerializer

    def post(self, request):
        serializer = OAuthExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        invitation_token = values.get("invitation_token", "")
        invitation = get_active_invitation(invitation_token)
        if invitation_token and invitation is None:
            return Response(
                {"detail": "This workspace invitation is invalid or expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if invitation is not None and values["account_type"] != AccountProfile.AccountTypeEnum.COMPANY:
            return Response(
                {"detail": "This invitation is for a company workspace account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            verified_identity = exchange_oauth_code(
                provider=values["provider"],
                code=values["code"],
                redirect_uri=values["redirect_uri"],
                nonce=values["nonce"],
            )
        except (OAuthExchangeError, httpx.HTTPError) as exc:
            logger.warning(
                "OAuth exchange failed for %s redirect_uri=%s: %s",
                values["provider"],
                values["redirect_uri"],
                exc,
            )
            return Response(
                {"detail": "The identity provider could not verify this sign-in."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        digest = subject_digest(verified_identity.provider, verified_identity.subject)
        identity = (
            SocialIdentity.objects.select_related("user__signacore_profile")
            .filter(
                provider=verified_identity.provider,
                subject_hash=digest,
            )
            .first()
        )
        is_new = identity is None
        linked_profile = None

        if is_new:
            linked_profile = (
                AccountProfile.objects.select_related("user")
                .filter(email_hash=email_digest(verified_identity.email))
                .first()
            )
            if linked_profile is not None and linked_profile.account_type != values["account_type"]:
                return Response(
                    {"detail": "This email is already connected to another SignaCore account."},
                    status=status.HTTP_409_CONFLICT,
                )
            if linked_profile is not None:
                is_new = False

        if identity is None and linked_profile is None and values["intent"] == OAuthIntentEnum.LOGIN:
            return Response(
                {"detail": "No SignaCore account is connected to this provider."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if (
            is_new
            and values["account_type"] == AccountProfile.AccountTypeEnum.COMPANY
            and invitation is None
            and not values.get("company_name", "").strip()
        ):
            return Response(
                {"company_name": ["Company name is required for a new company account."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if identity is None and linked_profile is not None:
                user = linked_profile.user
                if not user.is_active:
                    user.is_active = True
                    user.save(update_fields=["is_active"])
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
                if invitation is not None:
                    OrganizationMembership.objects.get_or_create(
                        organization=invitation.organization,
                        user=user,
                        defaults={"role": invitation.role},
                    )
                    invitation.accepted_at = timezone.now()
                    invitation.save(update_fields=["accepted_at"])
                    enqueue_task(sync_organization_seat_quantity, str(invitation.organization_id))
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
                if invitation is not None:
                    OrganizationMembership.objects.create(
                        organization=invitation.organization,
                        user=user,
                        role=invitation.role,
                    )
                    invitation.accepted_at = timezone.now()
                    invitation.save(update_fields=["accepted_at"])
                    enqueue_task(sync_organization_seat_quantity, str(invitation.organization_id))
                elif account_type == AccountProfile.AccountTypeEnum.COMPANY:
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

                if invitation is not None:
                    OrganizationMembership.objects.get_or_create(
                        organization=invitation.organization,
                        user=user,
                        defaults={"role": invitation.role},
                    )
                    invitation.accepted_at = timezone.now()
                    invitation.save(update_fields=["accepted_at"])
                    enqueue_task(sync_organization_seat_quantity, str(invitation.organization_id))

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


class OrganizationMembersView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    serializer_class = OrganizationInvitationSerializer

    def get(self, request):
        actor = get_admin_actor(request)
        organization = get_actor_organization(request, actor)
        if actor is None or organization is None:
            return Response({"detail": "Company workspace access is required."}, status=status.HTTP_403_FORBIDDEN)
        if (
            not actor.is_superuser
            and not OrganizationMembership.objects.filter(
                organization=organization,
                user=actor,
                status=OrganizationMembership.StatusEnum.ACTIVE,
            ).exists()
        ):
            return Response({"detail": "Company workspace access is required."}, status=status.HTTP_403_FORBIDDEN)
        log_account_event(
            request, actor, AdminAuditLog.ActionEnum.ORGANIZATION_MEMBER_LIST, "Viewed workspace members."
        )
        return Response({"items": serialize_organization_members(organization)}, status=status.HTTP_200_OK)

    def post(self, request):
        actor = get_admin_actor(request)
        organization = get_actor_organization(request, actor)
        membership = (
            OrganizationMembership.objects.filter(
                organization=organization,
                user=actor,
                status=OrganizationMembership.StatusEnum.ACTIVE,
            ).first()
            if actor and organization
            else None
        )
        if (
            actor is None
            or organization is None
            or (
                not actor.is_superuser
                and (
                    membership is None
                    or membership.role
                    not in {
                        OrganizationMembership.RoleEnum.OWNER,
                        OrganizationMembership.RoleEnum.ADMIN,
                    }
                )
            )
        ):
            return Response(
                {"detail": "Workspace owner or admin access is required."}, status=status.HTTP_403_FORBIDDEN
            )

        require_feature(actor, organization, PlanFeatureEnum.TEAM_MANAGEMENT)

        serializer = OrganizationInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        role = serializer.validated_data["role"]
        profile = AccountProfile.objects.select_related("user").filter(email_hash=email_digest(email)).first()
        if profile and profile.account_type != AccountProfile.AccountTypeEnum.COMPANY:
            return Response({"detail": "Only company accounts can join a workspace."}, status=status.HTTP_409_CONFLICT)
        if profile and OrganizationMembership.objects.filter(organization=organization, user=profile.user).exists():
            return Response({"detail": "This account is already a workspace member."}, status=status.HTTP_409_CONFLICT)
        if OrganizationInvitation.objects.filter(
            organization=organization,
            email_hash=email_digest(email),
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).exists():
            return Response(
                {"detail": "An invitation is already pending for this email."}, status=status.HTTP_409_CONFLICT
            )

        token = secrets.token_urlsafe(32)
        invitation = OrganizationInvitation.objects.create(
            organization=organization,
            invited_by=actor,
            email=email,
            role=role,
            token_hash=invitation_token_digest(token),
            expires_at=timezone.now() + timedelta(days=7),
        )
        log_account_event(
            request,
            actor,
            AdminAuditLog.ActionEnum.ORGANIZATION_MEMBER_INVITE,
            f"Invited {email} to {organization.name}.",
        )
        enqueue_task(
            send_organization_invitation,
            email,
            organization.name,
            actor.get_full_name() or actor.username,
            role,
            token,
        )
        serialized_members = serialize_organization_members(organization)
        invited_item = next(item for item in serialized_members if str(item["id"]) == str(invitation.id))
        return Response(
            {"detail": "Invitation sent.", "item": invited_item},
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request, membership_id):
        actor = get_admin_actor(request)
        organization = get_actor_organization(request, actor)
        membership = (
            OrganizationMembership.objects.filter(
                id=membership_id,
                organization=organization,
                status=OrganizationMembership.StatusEnum.ACTIVE,
            )
            .select_related("user")
            .first()
            if actor and organization
            else None
        )
        actor_membership = (
            OrganizationMembership.objects.filter(
                organization=organization,
                user=actor,
                status=OrganizationMembership.StatusEnum.ACTIVE,
            ).first()
            if actor and organization
            else None
        )
        if (
            actor is None
            or organization is None
            or (
                not actor.is_superuser
                and (
                    actor_membership is None
                    or actor_membership.role
                    not in {
                        OrganizationMembership.RoleEnum.OWNER,
                        OrganizationMembership.RoleEnum.ADMIN,
                    }
                )
            )
        ):
            return Response(
                {"detail": "Workspace owner or admin access is required."}, status=status.HTTP_403_FORBIDDEN
            )
        require_feature(actor, organization, PlanFeatureEnum.TEAM_MANAGEMENT)
        if membership is None:
            return Response({"detail": "Workspace member not found."}, status=status.HTTP_404_NOT_FOUND)
        if membership.role == OrganizationMembership.RoleEnum.OWNER:
            return Response({"detail": "The workspace owner cannot be removed."}, status=status.HTTP_400_BAD_REQUEST)
        membership.status = OrganizationMembership.StatusEnum.SUSPENDED
        membership.save(update_fields=["status", "updated_at"])
        enqueue_task(sync_organization_seat_quantity, str(organization.id))
        log_account_event(
            request, actor, AdminAuditLog.ActionEnum.ORGANIZATION_MEMBER_REMOVE, "Removed a workspace member."
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(operation_id="organization_members_list"),
    post=extend_schema(operation_id="organization_members_invite"),
)
class OrganizationMembersCollectionView(OrganizationMembersView):
    pass


@extend_schema_view(
    delete=extend_schema(operation_id="organization_member_remove"),
)
class OrganizationMemberDetailView(OrganizationMembersView):
    http_method_names = ["delete", "options"]


class AccountSigningRequestsView(APIView):
    authentication_classes = []
    permission_classes = [HasValidSignacoreSecret]
    throttle_classes = [SignacoreRateThrottle]
    throttle_scope = "account_data"
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
