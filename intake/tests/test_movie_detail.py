"""Movie detail page and write actions."""
from __future__ import annotations

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import CompletionEvent, Movie

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="movie-user", password="p")


@pytest.fixture
def movie():
    return Item.objects.create(
        source="tmdb",
        media_type=MediaType.MOVIE.value,
        media_id="990001",
        title="Movie Detail Test",
        synopsis="A test movie.",
        runtime=101,
        max_progress=1,
        metadata_complete=True,
        raw={"genres": [{"name": "Drama"}]},
    )


def test_movie_uses_dedicated_detail_page(client, user, movie):
    client.force_login(user)
    response = client.get(
        reverse("tracking:details", args=(movie.media_id,))
    )
    body = response.content.decode()
    assert response.status_code == 200
    assert "WATCH HISTORY" in body
    assert "YOUR RATING" in body
    assert "Update tracking" not in body
    date_input = body.split('id="movie-watched-on"', 1)[1].split(">", 1)[0]
    assert "value=" not in date_input


def test_movie_watch_rewatch_rate_and_unwatch(client, user, movie):
    client.force_login(user)
    watch_url = reverse("tracking:movie_mark_watched", args=(movie.media_id,))

    assert client.post(watch_url, {"watched_on": "2026-01-02"}).status_code == 200
    row = Movie.objects.get(user=user, item=movie)
    assert row.status == Status.COMPLETED.value
    assert row.end_date == date(2026, 1, 2)

    assert client.post(watch_url, {"watched_on": "2026-02-03"}).status_code == 200
    row.refresh_from_db()
    assert row.repeats == 1
    assert CompletionEvent.objects.filter(user=user, item=movie).count() == 2

    rate_url = reverse("tracking:movie_rate", args=(movie.media_id,))
    assert client.post(rate_url, {"score": "9"}).status_code == 200
    row.refresh_from_db()
    assert row.score == 9

    unwatch_url = reverse("tracking:movie_unwatch", args=(movie.media_id,))
    assert client.post(unwatch_url).status_code == 200
    row.refresh_from_db()
    assert row.status == Status.PLANNING.value
