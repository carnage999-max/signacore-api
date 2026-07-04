from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from signacore_api.strings import API_VERSION, HEALTH_STATUS_OK


class HealthCheckView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    def get(self, request):
        return Response({"status": HEALTH_STATUS_OK, "version": API_VERSION})

