"""Watchlist removal: explicit card ✕ and auto-promotion on watch."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from core.enums import Status
from core.models import Item
from tracking.models import MODEL_FOR_MEDIA_TYPE, TV, Episode
from tracking.services import record_completion

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def show_item():
    return Item.objects.create(
        source="tmdb",
        media_type="tv",
        media_id="42",
        title="Watched Show",
        metadata_complete=True,
        raw={"external_ids": {"imdb_id": "tt42"}},
    )


def _planning(user, item):
    model = MODEL_FOR_MEDIA_TYPE[item.media_type]
    return model.objects.create(
        user=user,
        item=item,
        status=Status.PLANNING.value,
    )


def test_remove_deletes_planning_row(client, user, show_item):
    _planning(user, show_item)

    resp = client.post(
        reverse("tracking:watchlist_remove", args=[show_item.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert resp.status_code == 200
    assert not TV.objects.filter(user=user, item=show_item).exists()
    assert "On watchlist" not in resp.content.decode()


def test_remove_resets_in_progress_row_instead_of_deleting(client, user, show_item):
    TV.objects.create(
        user=user,
        item=show_item,
        status=Status.IN_PROGRESS.value,
        progress=4,
    )

    resp = client.post(
        reverse("tracking:watchlist_remove", args=[show_item.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert resp.status_code == 200
    row = TV.objects.get(user=user, item=show_item)
    assert row.status == Status.IN_PROGRESS.value  # history preserved…
    assert "On watchlist" not in resp.content.decode()


def test_marking_episode_watched_promotes_show_out_of_planning(
    client,
    user,
    show_item,
):
    """Regression: watching an episode left the show on the watchlist."""
    _planning(user, show_item)
    season = Item.objects.create(
        source="tmdb",
        media_type="season",
        media_id="42",
        title="S1",
        season_number=1,
        parent=show_item,
        metadata_complete=True,
    )
    episode = Item.objects.create(
        source="tmdb",
        media_type="episode",
        media_id="42",
        title="Pilot",
        season_number=1,
        episode_number=1,
        parent=season,
        metadata_complete=True,
    )

    resp = client.post(reverse("tracking:toggle_episode", args=[42, 1, 1]), {})

    assert resp.status_code == 200
    show_row = TV.objects.get(user=user, item=show_item)
    assert show_row.status == Status.IN_PROGRESS.value
    assert Episode.objects.get(user=user, item=episode).status == Status.COMPLETED.value


def test_recording_episode_completion_promotes_planning_parents(user, show_item):
    _planning(user, show_item)
    season = Item.objects.create(
        source="tmdb", media_type="season", media_id="42", title="S1",
        season_number=1, parent=show_item,
    )
    _planning(user, season)
    episode = Item.objects.create(
        source="tmdb", media_type="episode", media_id="42", title="Pilot",
        season_number=1, episode_number=1, parent=season,
    )

    record_completion(user=user, item=episode)

    assert TV.objects.get(user=user, item=show_item).status == Status.IN_PROGRESS.value
    from tracking.models import Season
    assert Season.objects.get(user=user, item=season).status == Status.IN_PROGRESS.value


def test_movie_mark_watched_leaves_watchlist(client, user):
    movie = Item.objects.create(
        source="tmdb",
        media_type="movie",
        media_id="77",
        title="A Movie",
        metadata_complete=True,
    )
    _planning(user, movie)

    resp = client.post(reverse("tracking:movie_mark_watched", args=[77]))

    assert resp.status_code == 200
    from tracking.models import Movie

    assert not Movie.objects.filter(user=user, item=movie, status=Status.PLANNING.value).exists()
