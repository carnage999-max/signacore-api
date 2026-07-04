from django.urls import path

from .views import SignerContextView, SignerOtpSendView, SignerOtpVerifyView, SignerSubmitView


urlpatterns = [
    path("<uuid:token>/", SignerContextView.as_view(), name="signer-context"),
    path("<uuid:token>/otp/send/", SignerOtpSendView.as_view(), name="signer-otp-send"),
    path("<uuid:token>/otp/verify/", SignerOtpVerifyView.as_view(), name="signer-otp-verify"),
    path("<uuid:token>/submit/", SignerSubmitView.as_view(), name="signer-submit"),
]
