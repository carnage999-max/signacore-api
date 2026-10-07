"""Leaving the follow-up list.

Public and unauthenticated on purpose: the person using it has only ever been a recipient and has
no account to sign in to. A GET says what the link will do and a POST does it, so a mail client
prefetching the URL cannot unsubscribe somebody who never clicked.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from utils.unsubscribe import read_unsubscribe_token

from .models import EmailSuppression
from .serializers import UnsubscribeSerializer


class UnsubscribeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = UnsubscribeSerializer

    def get(self, request):
        """Whether this link is ours, without acting on it."""
        token = str(request.query_params.get("token", "")).strip()
        if not read_unsubscribe_token(token):
            return Response({"detail": "This link is not valid."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "Confirm to stop receiving these emails."}, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = UnsubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = read_unsubscribe_token(serializer.validated_data["token"])
        if not email:
            return Response({"detail": "This link is not valid."}, status=status.HTTP_400_BAD_REQUEST)

        EmailSuppression.suppress(email)
        # Deliberately not echoing the address back. The link is in an email that may be forwarded,
        # and whoever holds it should not learn an address they did not already have.
        return Response({"detail": "You will not receive these emails again."}, status=status.HTTP_200_OK)
