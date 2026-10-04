from django.urls import path

from .views import (
    AccountSigningRequestsView,
    EmailLoginView,
    EmailRegistrationView,
    EmailVerificationResendView,
    EmailVerificationView,
    OAuthExchangeView,
    OrganizationMemberDetailView,
    OrganizationMembersCollectionView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
)

urlpatterns = [
    path("oauth/exchange/", OAuthExchangeView.as_view(), name="oauth-exchange"),
    path("email/register/", EmailRegistrationView.as_view(), name="email-register"),
    path("email/login/", EmailLoginView.as_view(), name="email-login"),
    path("email/verify/", EmailVerificationView.as_view(), name="email-verify"),
    path("email/verify/resend/", EmailVerificationResendView.as_view(), name="email-verify-resend"),
    path("password/reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path("password/reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("account/signing-requests/", AccountSigningRequestsView.as_view(), name="account-signing-requests"),
    path("organization/members/", OrganizationMembersCollectionView.as_view(), name="organization-members"),
    path(
        "organization/members/<uuid:membership_id>/",
        OrganizationMemberDetailView.as_view(),
        name="organization-member-detail",
    ),
]
