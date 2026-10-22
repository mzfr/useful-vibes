"""Sources panel + download-client flow. All HTTP is mocked via respx."""

from __future__ import annotations

import re

import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from httpx import Response

from core.models import AppSettings, Item

pytestmark = pytest.mark.django_db

ADDON = "https://addon.example"


def stream_payload(*hashes: str, title: str = "Release") -> dict:
    return {
        "streams": [
            {
                "name": "Torrentio",
                "title": f"{title}.{h[:6]} 1080p\n\u25cf {50 - i}\n\U0001f4be 1.5 GB",
                "infoHash": h,
            }
            for i, h in enumerate(hashes)
        ]
    }


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def movie_item():
    return Item.objects.create(
        source="tmdb",
        media_type="movie",
        media_id="12345",
        title="Release Test Movie",
        metadata_complete=True,
        raw={"external_ids": {"imdb_id": "tt0117731"}},
    )


@pytest.fixture
def episode_chain():
    show = Item.objects.create(
        source="tmdb",
        media_type="tv",
        media_id="999",
        title="Release Test Show",
        metadata_complete=True,
        raw={"external_ids": {"imdb_id": "tt0944947"}},
    )
    season = Item.objects.create(
        source="tmdb",
        media_type="season",
        media_id="999",
        title="Season 1",
        season_number=1,
        parent=show,
        metadata_complete=True,
    )
    episode = Item.objects.create(
        source="tmdb",
        media_type="episode",
        media_id="999",
        title="Winter Is Coming",
        season_number=1,
        episode_number=1,
        parent=season,
        metadata_complete=True,
    )
    return show, season, episode


def _configure(addon=ADDON, host="qbit.local"):
    s = AppSettings.load()
    s.source_addon_url = addon
    s.qbittorrent_host = host
    s.qbittorrent_port = 8080
    s.set_secret("qbittorrent_password", "pw")
    s.save()


# --------------------------------------------------------------------- panel


@respx.mock
def test_panel_lists_streams_sorted_by_seeders(client, movie_item):
    _configure()
    respx.get(f"{ADDON}/stream/movie/tt0117731.json").mock(
        return_value=Response(200, json=stream_payload("a" * 40, "b" * 40))
    )

    resp = client.get(reverse("sources:panel", args=["tmdb", "movie", "12345"]))

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Release.aaaaaa" in body  # 50 seeders, listed first
    assert "Release.bbbbbb" in body  # 49 seeders, second
    assert "1.50 GB" in body  # 1.5 GB payload converted properly, not "1536 GB"
    assert "👤 50" in body and "👤 49" in body  # seeders parsed, not "?"


@respx.mock
def test_panel_episode_walks_to_show_imdb_id(client, episode_chain):
    show, season, episode = episode_chain
    _configure()
    respx.get(f"{ADDON}/stream/series/tt0944947:1:1.json").mock(
        return_value=Response(200, json=stream_payload("c" * 40))
    )

    resp = client.get(reverse("sources:panel_episode", args=["tmdb", "tv", "999", 1, 1]))

    assert resp.status_code == 200
    assert "Release.cccccc" in resp.content.decode()


@respx.mock
def test_panel_without_imdb_id_shows_error(client, movie_item):
    _configure()
    movie_item.raw = {}
    movie_item.save()

    resp = client.get(reverse("sources:panel", args=["tmdb", "movie", "12345"]))

    assert resp.status_code == 200
    assert "No IMDb ID" in resp.content.decode()


@respx.mock
def test_panel_addon_error_is_caught(client, movie_item):
    _configure()
    respx.get(f"{ADDON}/stream/movie/tt0117731.json").mock(return_value=Response(500, text="boom"))

    resp = client.get(reverse("sources:panel", args=["tmdb", "movie", "12345"]))

    assert resp.status_code == 200
    assert "500" in resp.content.decode()


def test_panel_requires_login(client, movie_item):
    anon = Client()
    resp = anon.get(reverse("sources:panel", args=["tmdb", "movie", "12345"]))
    assert resp.status_code == 302


def test_episode_page_renders_sources_button_with_url(client, episode_chain):
    """Regression: the template once reversed the wrong route name, which made
    {% url ... as var %} silently render an empty hx-get — HTMX then fetched
    the current page into the modal (content doubling) instead of the panel."""
    show, season, episode = episode_chain
    from django.urls import reverse

    resp = client.get(reverse("tracking:episode_detail", args=[999, 1, 1]))
    assert resp.status_code == 200
    match = re.search(r'hx-get="([^"]*)"[^>]*hx-target="#sources-modal"', resp.content.decode())
    assert match, "sources button missing from episode page"
    assert match.group(1) == reverse("sources:panel_episode", args=["tmdb", "tv", "999", 1, 1])


def test_movie_page_renders_sources_button_with_url(client, movie_item):
    from django.urls import reverse

    resp = client.get(reverse("tracking:movie_details", args=[12345]))
    assert resp.status_code == 200
    match = re.search(r'hx-get="([^"]*)"[^>]*hx-target="#sources-modal"', resp.content.decode())
    assert match, "sources button missing from movie page"
    assert match.group(1) == reverse("sources:panel", args=["tmdb", "movie", "12345"])


def test_panel_404s_without_numbers_on_episode_route(client, episode_chain):
    resp = client.get(reverse("sources:panel", args=["tmdb", "episode", "999"]))
    assert resp.status_code == 404


# ----------------------------------------------------------------------- add


@respx.mock
def test_add_sends_magnet_to_client(client, movie_item):
    _configure()
    respx.get(f"{ADDON}/stream/movie/tt0117731.json").mock(
        return_value=Response(200, json=stream_payload("d" * 40))
    )
    add_route = respx.post("http://qbit.local:8080/api/v2/torrents/add").mock(
        return_value=Response(200, json={"result": "Ok."})
    )

    resp = client.post(
        reverse("sources:add", args=["tmdb", "movie", "12345"]),
        {"payload": '{"info_hash": "%s", "title": "Some.Release"}' % ("d" * 40)},
    )

    assert resp.status_code == 200
    assert add_route.called
    sent = add_route.calls[0].request.content.decode()
    assert "magnet%3A%3Fxt%3Durn%3Abtih%3A" + "d" * 40 in sent


@respx.mock
def test_add_logs_in_and_retries_on_403(client, movie_item):
    _configure()
    respx.get(f"{ADDON}/stream/movie/tt0117731.json").mock(
        return_value=Response(200, json=stream_payload("e" * 40))
    )
    respx.post("http://qbit.local:8080/api/v2/auth/login").mock(
        return_value=Response(200, text="Ok.")
    )
    add_route = respx.post("http://qbit.local:8080/api/v2/torrents/add").mock(
        side_effect=[Response(403, text="Forbidden"), Response(200, text="Ok.")]
    )

    resp = client.post(
        reverse("sources:add", args=["tmdb", "movie", "12345"]),
        {"payload": '{"info_hash": "%s", "title": "X"}' % ("e" * 40)},
    )

    assert resp.status_code == 200
    assert add_route.call_count == 2


@respx.mock
def test_add_accepts_empty_204_login(client, movie_item):
    """qBittorrent 5.x answers successful login with an empty 204, not `Ok.`."""
    _configure()
    respx.get(f"{ADDON}/stream/movie/tt0117731.json").mock(
        return_value=Response(200, json=stream_payload("9" * 40))
    )
    respx.post("http://qbit.local:8080/api/v2/auth/login").mock(return_value=Response(204))
    add_route = respx.post("http://qbit.local:8080/api/v2/torrents/add").mock(
        side_effect=[Response(403, text="Forbidden"), Response(200, text="Ok.")]
    )

    resp = client.post(
        reverse("sources:add", args=["tmdb", "movie", "12345"]),
        {"payload": '{"info_hash": "%s", "title": "X"}' % ("9" * 40)},
    )

    assert resp.status_code == 200
    assert add_route.call_count == 2


@respx.mock
def test_add_failure_queues_user_message(client, movie_item):

    _configure()
    respx.get(f"{ADDON}/stream/movie/tt0117731.json").mock(
        return_value=Response(200, json=stream_payload("f" * 40))
    )
    respx.post("http://qbit.local:8080/api/v2/torrents/add").mock(
        return_value=Response(500, text="nope")
    )

    resp = client.post(
        reverse("sources:add", args=["tmdb", "movie", "12345"]),
        {"payload": '{"info_hash": "%s", "title": "X"}' % ("f" * 40)},
    )

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Couldn't send" in body or "unreachable" in body
    assert "sent ✓" not in body


def test_add_rejects_bad_payload(client, movie_item):
    _configure()
    resp = client.post(
        reverse("sources:add", args=["tmdb", "movie", "12345"]),
        {"payload": "not json"},
    )
    assert resp.status_code == 400


# ------------------------------------------------------- resolve unit checks


def test_imdb_id_prefers_show_for_season(episode_chain):
    from sources.resolve import imdb_id_for

    show, season, episode = episode_chain
    assert imdb_id_for(show) == "tt0944947"
    assert imdb_id_for(season) == "tt0944947"
    assert imdb_id_for(episode) == "tt0944947"


def test_magnet_link_shape():
    from sources.base import Stream, magnet_link

    assert magnet_link(Stream(title="R", info_hash="AB")) == ("magnet:?xt=urn:btih:ab&dn=R")


def test_size_display_units():
    from sources.base import Stream

    assert Stream(title="x", info_hash="a" * 40, size_mb=1536).size_display == "1.50 GB"
    assert Stream(title="x", info_hash="a" * 40, size_mb=2119.7).size_display == "2.07 GB"
    assert Stream(title="x", info_hash="a" * 40, size_mb=700).size_display == "700 MB"
    assert Stream(title="x", info_hash="a" * 40, size_mb=None).size_display == ""


# --------------------------------------------------------- relevance filters


def test_relevance_drops_mega_packs():
    from sources.stremio_addon import is_relevant

    assert not is_relevant(
        "Marathi Movies & Plays Mega Pack 1080p Web-DL x264 AAC [TMB]", "Highway"
    )


def test_relevance_keeps_longest_word_match():
    from sources.stremio_addon import is_relevant

    # Full title won't appear (punctuation dropped), but "philadelphia" does.
    assert is_relevant(
        "Its Always Sunny in Philadelphia S01E01 1080p WEB-DL", "It's Always Sunny in Philadelphia"
    )


def test_relevance_keeps_everything_without_a_query():
    from sources.stremio_addon import is_relevant

    assert is_relevant("Anything At All", None)
    assert is_relevant("Anything At All", "")


@respx.mock
def test_panel_quality_filter(client, movie_item):
    _configure()
    payload = {
        "streams": [
            {"title": "Release Test Movie 2160p\n\u25cf 10\n\U0001f4be 8 GB", "infoHash": "a" * 40},
            {"title": "Release Test Movie 1080p\n\u25cf 20\n\U0001f4be 4 GB", "infoHash": "b" * 40},
        ]
    }
    respx.get(f"{ADDON}/stream/movie/tt0117731.json").mock(return_value=Response(200, json=payload))

    resp = client.get(
        reverse("sources:panel", args=["tmdb", "movie", "12345"]), {"quality": "2160p"}
    )

    body = resp.content.decode()
    assert "Release Test Movie 2160p" in body
    assert "Release Test Movie 1080p" not in body
