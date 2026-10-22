from django.urls import path

from . import collections, views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("search/remote", views.remote_search, name="remote_search"),
    path("history/", views.history, name="history"),
    path("watchlist/", collections.watchlist, name="watchlist"),
    path("discontinued/", collections.discontinued, name="discontinued"),
    path(
        "discontinued/<int:item_id>/restore",
        collections.restore_discontinued,
        name="restore_discontinued",
    ),
    path("favorites/", collections.favorites, name="favorites"),
    path("favorites/<int:item_id>/toggle", collections.toggle_favorite, name="toggle_favorite"),
]
