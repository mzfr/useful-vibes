from django.urls import path

from . import views

app_name = "releases"

urlpatterns = [
    path("", views.index, name="index"),
    path("seen", views.mark_seen, name="mark_seen"),
]
