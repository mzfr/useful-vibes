"""Canonical public media URLs and legacy redirects."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from core.enums import MediaType
from core.models import Item

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="clean-url-user")


def _root(media_type: str, media_id: str, title: str) -> Item:
    return Item.objects.create(
        source="tmdb", media_type=media_type, media_id=media_id,
        title=title, metadata_complete=True,
    )


def test_clean_show_movie_and_episode_urls(client, user):
    show = _root(MediaType.TV.value, "880001", "Clean Show")
    movie = _root(MediaType.MOVIE.value, "880002", "Clean Movie")
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id=show.media_id,
        season_number=8, parent=show, title="Season 8", metadata_complete=True,
    )
    episode = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id=show.media_id,
        season_number=8, episode_number=3, parent=season, title="Episode 3",
        metadata_complete=True,
    )
    client.force_login(user)

    assert show.get_absolute_url() == "/880001/"
    assert movie.get_absolute_url() == "/880002/"
    assert episode.get_absolute_url() == "/880001/season/8/episode/3/"
    assert client.get(show.get_absolute_url()).status_code == 200
    assert client.get(movie.get_absolute_url()).status_code == 200
    assert client.get(episode.get_absolute_url()).status_code == 200


def test_legacy_tmdb_urls_redirect_permanently(client, user):
    show = _root(MediaType.TV.value, "880003", "Legacy Show")
    client.force_login(user)
    response = client.get("/items/tmdb/tv/880003/")
    assert response.status_code == 301
    assert response.url == show.get_absolute_url()


def test_tmdb_namespace_collision_keeps_both_items_reachable(client, user):
    show = _root(MediaType.TV.value, "880004", "Collision Show")
    movie = _root(MediaType.MOVIE.value, "880004", "Collision Movie")
    client.force_login(user)

    assert show.get_absolute_url() == "/880004/"
    assert movie.get_absolute_url() == "/880004/movie/"
    assert "Collision Show" in client.get(show.get_absolute_url()).content.decode()
    assert "Collision Movie" in client.get(movie.get_absolute_url()).content.decode()
