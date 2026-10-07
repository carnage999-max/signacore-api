from django.urls import path

from .views import UnsubscribeView

urlpatterns = [
    path("unsubscribe/", UnsubscribeView.as_view(), name="unsubscribe"),
]
