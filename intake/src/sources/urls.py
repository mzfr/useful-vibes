"""URLs live under the neutral `/sources/` prefix."""

from django.urls import path

from . import views

app_name = "sources"

urlpatterns = [
    path(
        "sources/add/<str:source>/<str:media_type>/<str:media_id>",
        views.add,
        name="add",
    ),
    path(
        "sources/add/<str:source>/<str:media_type>/<str:media_id>/<int:season_number>/<int:episode_number>",
        views.add,
        name="add_episode",
    ),
    path(
        "sources/<str:source>/<str:media_type>/<str:media_id>",
        views.panel,
        name="panel",
    ),
    path(
        "sources/<str:source>/<str:media_type>/<str:media_id>/<int:season_number>/<int:episode_number>",
        views.panel,
        name="panel_episode",
    ),
]
