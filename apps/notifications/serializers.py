from rest_framework import serializers


class UnsubscribeSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)
