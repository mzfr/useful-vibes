"""TMDB provider tests — fully offline, respx intercepts httpx calls."""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from providers.base import NotSupported
from providers.tmdb import TMDBProvider


@pytest.fixture
def tmdb():
    # Construct directly with a fake key so we don't depend on settings.
    return TMDBProvider(api_key="fake-test-key")


@respx.mock
def test_search_movie(tmdb, load_fixture):
    respx.get("https://api.themoviedb.org/3/search/movie").mock(
        return_value=Response(200, json=load_fixture("tmdb_search_movie.json"))
    )

    results = tmdb.search("movie", "inception")

    assert results.total_results == 2
    assert len(results.hits) == 2
    first = results.hits[0]
    assert first.source == "tmdb"
    assert first.media_type == "movie"
    assert first.media_id == "27205"
    assert first.title == "Inception"
    assert first.year == 2010
    assert "inception.jpg" in first.image


@respx.mock
def test_movie_details(tmdb, load_fixture):
    respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )

    md = tmdb.details("movie", "27205")

    assert md.title == "Inception"
    assert md.runtime == 148
    assert md.max_progress == 1
    assert md.release_date.isoformat() == "2010-07-15"
    assert "Action" in md.genres and "Science Fiction" in md.genres
    assert md.external_ids == {"imdb_id": "tt1375666"}

    # Cast keeps character role; crew filters to headline jobs only.
    assert md.cast[0].name == "Leonardo DiCaprio"
    assert md.cast[0].role == "Cobb"
    crew_jobs = {p.role for p in md.crew}
    assert crew_jobs == {"Director", "Writer"}  # composer filtered out


@respx.mock
def test_tv_details(tmdb, load_fixture):
    respx.get("https://api.themoviedb.org/3/tv/1399").mock(
        return_value=Response(200, json=load_fixture("tmdb_tv_details.json"))
    )

    md = tmdb.details("tv", "1399")

    assert md.title == "Game of Thrones"
    assert md.max_progress == 73
    assert md.runtime == 60
    assert md.release_date.isoformat() == "2011-04-17"


@respx.mock
def test_season(tmdb, load_fixture):
    respx.get("https://api.themoviedb.org/3/tv/1399/season/1").mock(
        return_value=Response(200, json=load_fixture("tmdb_season.json"))
    )

    season = tmdb.season("1399", 1)

    assert season.season_number == 1
    assert season.max_progress == 2
    assert len(season.episodes) == 2
    e1 = season.episodes[0]
    assert e1.episode_number == 1
    assert e1.season_number == 1
    assert e1.title == "Winter Is Coming"
    assert e1.runtime == 62


@respx.mock
def test_episode(tmdb, load_fixture):
    respx.get(
        "https://api.themoviedb.org/3/tv/1399/season/1/episode/1"
    ).mock(return_value=Response(200, json=load_fixture("tmdb_episode.json")))

    ep = tmdb.episode("1399", 1, 1)

    assert ep.episode_number == 1
    assert ep.season_number == 1
    assert ep.title == "Winter Is Coming"
    assert ep.max_progress == 1


def test_unsupported_media_type(tmdb):
    with pytest.raises(NotSupported):
        tmdb.search("book", "anything")
