from django.urls import path

from . import book_views, episode_detail, movie_detail, season_views, up_next_views, views

app_name = "tracking"

urlpatterns = [
    path("watchlist/item/<int:item_id>/toggle", views.watchlist_toggle, name="watchlist_toggle"),
    path(
        "watchlist/item/<int:item_id>/remove",
        views.remove_from_watchlist, name="watchlist_remove",
    ),
    path(
        "watchlist/add/<str:source>/<str:media_type>/<str:media_id>",
        views.add_remote,
        name="add_remote",
    ),

    # Books library + detail actions.
    path("books/", book_views.books_index, name="books"),
    path("books/search", book_views.book_search, name="book_search"),
    path("books/add", book_views.book_add, name="book_add"),
    path("books/<str:olid>/", book_views.book_detail, name="book_detail"),
    path("books/item/<int:item_id>/status", book_views.book_status, name="book_status"),
    path("books/item/<int:item_id>/progress", book_views.book_progress, name="book_progress"),
    path("books/item/<int:item_id>/dates", book_views.book_dates, name="book_dates"),
    path("books/item/<int:item_id>/rate", book_views.book_rate, name="book_rate"),

    # Clean public TMDB routes. Numeric IDs keep these from shadowing named
    # pages such as /stats/, /lists/, /admin/, and /api/.
    path("<int:media_id>/", views.clean_details, name="details"),
    path("<int:media_id>/movie/", views.clean_details, {"movie": True}, name="movie_details"),
    path(
        "<int:media_id>/season/<int:season_number>/episode/<int:episode_number>/",
        episode_detail.episode_detail,
        {"source": "tmdb"},
        name="episode_detail",
    ),

    # Legacy/provider-qualified routes. TMDB GETs permanently redirect to the
    # clean route; non-TMDB media (currently books) continue to render here.
    path("items/<str:source>/<str:media_type>/<str:media_id>/", views.legacy_details, name="legacy_details"),
    path(
        "items/<str:source>/tv/<str:media_id>/season/<int:season_number>/",
        views.legacy_season,
        name="legacy_season",
    ),
    path(
        "items/<str:source>/tv/<str:media_id>/season/<int:season_number>/episode/<int:episode_number>/",
        views.legacy_episode,
        name="legacy_episode",
    ),
    path("items/<str:source>/<str:media_type>/<str:media_id>/track", views.track, name="track"),
    path("<int:media_id>/visibility", views.show_visibility, {"source": "tmdb"}, name="show_visibility"),
    path("<int:media_id>/watchlist", views.toggle_watchlist, {"source": "tmdb"}, name="toggle_watchlist"),
    path("<int:media_id>/mark-next-watched", views.mark_show_next_watched, {"source": "tmdb"}, name="mark_show_next_watched"),
    path(
        "items/<str:source>/<str:media_type>/<str:media_id>/watched-again",
        views.watched_again,
        name="watched_again",
    ),

    # Movie detail writes
    path("<int:media_id>/watch", movie_detail.mark_watched, {"source": "tmdb"}, name="movie_mark_watched"),
    path("<int:media_id>/unwatch", movie_detail.unwatch, {"source": "tmdb"}, name="movie_unwatch"),
    path("<int:media_id>/rate", movie_detail.rate, {"source": "tmdb"}, name="movie_rate"),

    # Season + episode grid
    path("<int:media_id>/season/<int:season_number>/", season_views.season_detail, {"source": "tmdb"}, name="season"),
    path("<int:media_id>/season/<int:season_number>/panel/", season_views.season_panel, {"source": "tmdb"}, name="season_panel"),
    path("<int:media_id>/season/<int:season_number>/next/", season_views.new_season_next_episode, {"source": "tmdb"}, name="new_season_next_episode"),
    path(
        "<int:media_id>/season/<int:season_number>/ep/<int:episode_number>/toggle",
        season_views.toggle_episode,
        {"source": "tmdb"},
        name="toggle_episode",
    ),
    path(
        "<int:media_id>/season/<int:season_number>/ep/<int:episode_number>/unwatch",
        season_views.unwatch_episode,
        {"source": "tmdb"},
        name="unwatch_episode",
    ),
    path(
        "<int:media_id>/season/<int:season_number>/ep/<int:episode_number>/rate",
        season_views.rate_episode,
        {"source": "tmdb"},
        name="rate_episode",
    ),
    path(
        "<int:media_id>/season/<int:season_number>/bulk",
        season_views.bulk_season,
        {"source": "tmdb"},
        name="bulk_season",
    ),

    # Single episode writes share the clean episode prefix.
    path(
        "<int:media_id>/season/<int:season_number>/episode/<int:episode_number>/watch",
        episode_detail.episode_mark_watched,
        {"source": "tmdb"},
        name="episode_mark_watched",
    ),
    path(
        "<int:media_id>/season/<int:season_number>/episode/<int:episode_number>/unwatch",
        episode_detail.episode_unwatch,
        {"source": "tmdb"},
        name="episode_unwatch",
    ),
    path(
        "<int:media_id>/season/<int:season_number>/episode/<int:episode_number>/rate",
        episode_detail.episode_rate,
        {"source": "tmdb"},
        name="episode_rate",
    ),
    path(
        "<int:media_id>/season/<int:season_number>/episode/<int:episode_number>/note",
        episode_detail.episode_note,
        {"source": "tmdb"},
        name="episode_note",
    ),

    # Up Next
    path(
        "up-next/item/<int:item_id>/discontinue",
        up_next_views.discontinue_item,
        name="up_next_discontinue",
    ),
    path(
        "up-next/episode/<int:episode_item_id>/watched",
        up_next_views.mark_next_watched,
        name="up_next_mark_watched",
    ),
    path(
        "up-next/show/<int:show_item_id>/automatic-download/season/"
        "<int:start_season_number>/episode/<int:start_episode_number>",
        up_next_views.toggle_automatic_download,
        name="up_next_automatic_download",
    ),
    path(
        "up-next/<str:source>/tv/<str:media_id>/season/<int:season_number>/watched",
        up_next_views.mark_new_season_next_watched,
        name="up_next_new_season_watched",
    ),
    path(
        "up-next/item/<int:item_id>/finished",
        up_next_views.mark_item_finished,
        name="up_next_item_finished",
    ),
]
