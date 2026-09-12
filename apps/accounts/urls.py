from django.urls import path

from .views import AccountSigningRequestsView, OAuthExchangeView


urlpatterns = [
    path("oauth/exchange/", OAuthExchangeView.as_view(), name="oauth-exchange"),
    path("account/signing-requests/", AccountSigningRequestsView.as_view(), name="account-signing-requests"),
]
