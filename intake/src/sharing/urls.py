from django.urls import path

from . import views

app_name = "sharing"

urlpatterns = [
    path("settings/history-sharing/", views.manage, name="manage"),
    path("settings/history-sharing/new", views.create, name="create"),
    path("settings/history-sharing/origin", views.save_origin, name="save_origin"),
    path(
        "settings/history-sharing/sync-interval",
        views.save_sync_interval,
        name="save_sync_interval",
    ),
    path(
        "settings/history-sharing/<int:link_id>/revoke",
        views.revoke,
        name="revoke",
    ),
]
