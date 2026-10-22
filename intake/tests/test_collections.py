"""Watchlist and favorites: the two built-in collections."""
import pytest
from django.urls import reverse

from core.enums import MediaType, Status
from core.models import Favorite, Item
from tracking.models import TV, Movie


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user(username="collections", password="pw")


@pytest.fixture
def movie(db):
    return Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="700",
        title="Unreleased Thing",
    )


@pytest.fixture
def show(db):
    return Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="701", title="Some Show",
    )


def test_watchlist_lists_planning_rows_across_types(client, user, movie, show):
    Movie.objects.create(user=user, item=movie, status=Status.PLANNING.value)
    TV.objects.create(user=user, item=show, status=Status.PLANNING.value)
    client.force_login(user)

    body = client.get(reverse("core:watchlist")).content.decode()

    assert "Unreleased Thing" in body
    assert "Some Show" in body


def test_watchlist_excludes_watched_items(client, user, movie):
    Movie.objects.create(user=user, item=movie, status=Status.COMPLETED.value)
    client.force_login(user)

    assert "Unreleased Thing" not in client.get(reverse("core:watchlist")).content.decode()


def test_watchlist_type_filter(client, user, movie, show):
    Movie.objects.create(user=user, item=movie, status=Status.PLANNING.value)
    TV.objects.create(user=user, item=show, status=Status.PLANNING.value)
    client.force_login(user)

    body = client.get(reverse("core:watchlist"), {"type": "movie"}).content.decode()

    assert "Unreleased Thing" in body
    assert "Some Show" not in body


def test_watchlist_toggle_adds_then_removes(client, user, movie):
    client.force_login(user)
    url = reverse("tracking:watchlist_toggle", args=[movie.pk])

    client.post(url)
    assert Movie.objects.get(user=user, item=movie).status == Status.PLANNING.value

    client.post(url)
    assert not Movie.objects.filter(user=user, item=movie).exists()


def test_watchlist_toggle_keeps_watched_history(client, user, movie):
    """Adding a completed movie to the watchlist must not demote it."""
    Movie.objects.create(user=user, item=movie, status=Status.COMPLETED.value)
    client.force_login(user)

    client.post(reverse("tracking:watchlist_toggle", args=[movie.pk]))

    assert Movie.objects.get(user=user, item=movie).status == Status.COMPLETED.value


def test_favorite_toggle_round_trip(client, user, movie):
    client.force_login(user)
    url = reverse("core:toggle_favorite", args=[movie.pk])

    client.post(url)
    assert Favorite.objects.filter(user=user, item=movie).exists()

    client.post(url)
    assert not Favorite.objects.filter(user=user, item=movie).exists()


def test_favorites_page_includes_episodes(client, user, show):
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="701",
        season_number=1, title="Season 1", parent=show,
    )
    episode = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="701",
        season_number=1, episode_number=3, title="Pilot Plus", parent=season,
    )
    Favorite.objects.create(user=user, item=episode)
    client.force_login(user)

    body = client.get(reverse("core:favorites")).content.decode()

    assert "Pilot Plus" in body
    assert "S01E03" in body
    # The Shows filter must reach episodes, not just root TV rows.
    assert "Pilot Plus" in client.get(
        reverse("core:favorites"), {"type": "tv"}
    ).content.decode()
