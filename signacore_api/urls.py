from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.signing.views import SignerPortalView
from .api.health import HealthCheckView


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthCheckView.as_view(), name="health-check"),
    path("api/admin/", include("apps.documents.urls")),
    path("api/sign/", include("apps.signing.urls")),
    path("sign/<uuid:token>/", SignerPortalView.as_view(), name="signer-portal"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
