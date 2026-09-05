from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework.views import APIView

from signacore_api.strings import API_VERSION, HEALTH_STATUS_OK


class HealthCheckSerializer(serializers.Serializer):
    status = serializers.CharField()
    version = serializers.CharField()


class HealthCheckView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []
    serializer_class = HealthCheckSerializer

    def get(self, request):
        return Response({"status": HEALTH_STATUS_OK, "version": API_VERSION})
