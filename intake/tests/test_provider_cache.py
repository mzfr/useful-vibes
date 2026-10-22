"""Provider caching: prove the second call doesn't hit the network."""
from __future__ import annotations

import pytest
import respx
from django.test import override_settings
from httpx import Response

from providers import registry
from providers.cache import CachedProvider


@pytest.fixture(autouse=True)
def reset_provider_cache():
    registry.reset_cache()
    yield
    registry.reset_cache()


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_details_call_is_cached_after_first_hit(load_fixture):
    """Two calls, one upstream request — the cache absorbs the second."""
    route = respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )

    tmdb = registry.get("tmdb")
    assert isinstance(tmdb, CachedProvider)

    a = tmdb.details("movie", "27205")
    b = tmdb.details("movie", "27205")

    assert route.call_count == 1, (
        f"expected exactly 1 upstream call, got {route.call_count}"
    )
    assert a.title == b.title == "Inception"


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_season_call_is_cached(load_fixture):
    route = respx.get(
        "https://api.themoviedb.org/3/tv/1399/season/1"
    ).mock(return_value=Response(200, json=load_fixture("tmdb_season.json")))

    tmdb = registry.get("tmdb")
    tmdb.season("1399", 1)
    tmdb.season("1399", 1)
    tmdb.season("1399", 1)

    assert route.call_count == 1


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_different_ids_dont_collide(load_fixture):
    """Cache key includes the media_id — distinct items hit the network."""
    a = respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )
    b = respx.get("https://api.themoviedb.org/3/movie/157336").mock(
        return_value=Response(200, json={
            "id": 157336, "title": "Interstellar", "poster_path": None,
            "release_date": "2014-11-05", "runtime": 169, "genres": [],
            "credits": {"cast": [], "crew": []}, "external_ids": {},
        })
    )

    tmdb = registry.get("tmdb")
    tmdb.details("movie", "27205")
    tmdb.details("movie", "157336")
    tmdb.details("movie", "27205")  # cached
    tmdb.details("movie", "157336")  # cached

    assert a.call_count == 1
    assert b.call_count == 1


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_search_is_cached(load_fixture):
    route = respx.get("https://api.themoviedb.org/3/search/movie").mock(
        return_value=Response(200, json=load_fixture("tmdb_search_movie.json"))
    )
    tmdb = registry.get("tmdb")
    tmdb.search("movie", "Inception")
    tmdb.search("movie", "INCEPTION")  # normalized → same key
    tmdb.search("movie", "  inception  ")  # normalized → same key
    assert route.call_count == 1


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_reset_cache_clears_responses(load_fixture):
    route = respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )
    tmdb = registry.get("tmdb")
    tmdb.details("movie", "27205")
    registry.reset_cache()
    tmdb_again = registry.get("tmdb")
    tmdb_again.details("movie", "27205")
    assert route.call_count == 2


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_invalidate_drops_just_one_entry(load_fixture):
    route = respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )
    tmdb = registry.get("tmdb")
    tmdb.details("movie", "27205")
    tmdb.invalidate("movie", "27205")
    tmdb.details("movie", "27205")
    assert route.call_count == 2
