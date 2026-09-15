from django.urls import path

from .views import (
    AccountSigningRequestsView,
    EmailLoginView,
    EmailRegistrationView,
    EmailVerificationView,
    OAuthExchangeView,
    OrganizationMemberDetailView,
    OrganizationMembersCollectionView,
)


urlpatterns = [
    path("oauth/exchange/", OAuthExchangeView.as_view(), name="oauth-exchange"),
    path("email/register/", EmailRegistrationView.as_view(), name="email-register"),
    path("email/login/", EmailLoginView.as_view(), name="email-login"),
    path("email/verify/", EmailVerificationView.as_view(), name="email-verify"),
    path("account/signing-requests/", AccountSigningRequestsView.as_view(), name="account-signing-requests"),
    path("organization/members/", OrganizationMembersCollectionView.as_view(), name="organization-members"),
    path("organization/members/<uuid:membership_id>/", OrganizationMemberDetailView.as_view(), name="organization-member-detail"),
]
