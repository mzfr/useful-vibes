from django.urls import path

from .api import api

urlpatterns = [
    path("api/v1/", api.urls),
]
