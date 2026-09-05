from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView
from rest_framework.permissions import IsAdminUser

from apps.signing.views import SignerPortalView
from .api.health import HealthCheckView
from .docs import SignacoreApiDocsView


admin.site.site_header = "SignaCore Admin"
admin.site.site_title = "SignaCore Admin"
admin.site.index_title = "Operations console"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthCheckView.as_view(), name="health-check"),
    path("api/schema/", SpectacularAPIView.as_view(permission_classes=[IsAdminUser]), name="schema"),
    path("api/docs/", staff_member_required(SignacoreApiDocsView.as_view()), name="api-docs"),
    path("api/redoc/", staff_member_required(SpectacularRedocView.as_view(url_name="schema")), name="api-redoc"),
    path("api/admin/", include("apps.documents.urls")),
    path("api/sign/", include("apps.signing.urls")),
    path("sign/<uuid:token>/", SignerPortalView.as_view(), name="signer-portal"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
