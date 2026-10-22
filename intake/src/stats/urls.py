from django.urls import path

from . import views

app_name = "stats"

urlpatterns = [
    path("stats/", views.index, name="index"),
    path("stats/<int:year>/", views.year, name="year"),
]
