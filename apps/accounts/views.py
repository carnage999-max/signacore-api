from __future__ import annotations

import hashlib
import hmac

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.documents.auth import HasValidSignacoreSecret
from apps.documents.models import AdminAuditLog
from apps.documents.views import get_request_ip
from apps.signing.models import SigningRequest
from services.oauth_client import OAuthExchangeError, exchange_oauth_code
from utils.identity import email_digest

from .models import AccountProfile, OAuthIntentEnum, Organization, OrganizationMembership, SocialIdentity
from .serializers import AccountSessionSerializer, AccountSigningRequestSerializer, OAuthExchangeSerializer


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

        if is_new and values["intent"] == OAuthIntentEnum.LOGIN:
            return Response(
                {"detail": "No SignaCore account is connected to this provider."},
                status=status.HTTP_404_NOT_FOUND,
            )

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
            if identity is None:
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

            SigningRequest.objects.filter(
                signer_email_hash=email_digest(verified_identity.email),
                signer_user__isnull=True,
            ).update(signer_user=user)

        payload = build_account_payload(user, is_new=is_new)
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
