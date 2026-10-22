"""REST API surface — auth, search, tracking, lists, exports, token mint."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from httpx import Response

from api.models import APIToken
from core.enums import MediaType, Status
from core.models import Item
from providers import registry
from tracking.models import CompletionEvent, Episode, Movie, Season

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def reset_provider_cache():
    registry.reset_cache()
    yield
    registry.reset_cache()


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def session_client(user):
    c = Client()
    c.force_login(user)
    return c


# ---------------------------------------------------------------------- auth


def test_unauthenticated_request_rejected():
    resp = Client().get("/api/v1/me/up-next")
    assert resp.status_code == 401


def test_token_mint_and_use(session_client, user):
    resp = session_client.post(
        "/api/v1/me/token",
        data=json.dumps({"ttl": "1w", "label": "test"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    raw = resp.json()["token"]
    assert raw.startswith("intake_")
    # Only one token per user.
    assert APIToken.objects.filter(user=user).count() == 1

    # Use the token in a fresh client (no session).
    fresh = Client()
    resp = fresh.get("/api/v1/me/up-next", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 200


def test_token_mint_revokes_previous(session_client, user):
    session_client.post("/api/v1/me/token", data=json.dumps({"ttl": "1d"}),
                        content_type="application/json")
    first = APIToken.objects.get(user=user)
    session_client.post("/api/v1/me/token", data=json.dumps({"ttl": "1d"}),
                        content_type="application/json")
    second = APIToken.objects.get(user=user)
    assert first.digest != second.digest


def test_expired_token_rejected(user):
    token, raw = APIToken.mint(user, ttl="1d")
    APIToken.objects.filter(pk=token.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    resp = Client().get("/api/v1/me/up-next", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 401


# --------------------------------------------------------------------- search


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_search_endpoint(session_client, load_fixture):
    respx.get("https://api.themoviedb.org/3/search/movie").mock(
        return_value=Response(200, json=load_fixture("tmdb_search_movie.json"))
    )
    resp = session_client.get("/api/v1/search", {"q": "inception", "type": "movie"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_results"] == 2
    assert body["hits"][0]["title"] == "Inception"


# -------------------------------------------------------------------- tracking


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_tracking_upsert_then_progress(session_client, user, load_fixture):
    respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )

    resp = session_client.post(
        "/api/v1/tracking/movie",
        data=json.dumps({
            "source": "tmdb", "media_id": "27205",
            "status": "completed", "score": 9,
        }),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == 9
    assert body["status"] == "completed"
    assert Movie.objects.filter(user=user, item__media_id="27205").exists()

    tracking_id = body["id"]
    resp = session_client.post(
        f"/api/v1/tracking/movie/{tracking_id}/progress",
        data=json.dumps({"progress": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json()["progress"] == 1


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_tracking_rewatch(session_client, user, load_fixture):
    respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )
    r = session_client.post(
        "/api/v1/tracking/movie",
        data=json.dumps({"source": "tmdb", "media_id": "27205", "status": "completed"}),
        content_type="application/json",
    )
    tid = r.json()["id"]
    resp = session_client.post(f"/api/v1/tracking/movie/{tid}/rewatch")
    assert resp.status_code == 200
    assert resp.json()["repeats"] == 1


# ----------------------------------------------------------------- bulk season


def _build_show_in_db():
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="42", title="X"
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="42",
        season_number=1, parent=show, title="S1",
    )
    for n in range(1, 4):
        Item.objects.create(
            source="tmdb", media_type=MediaType.EPISODE.value, media_id="42",
            season_number=1, episode_number=n, parent=season, title=f"E{n}",
        )
    return show, season


def test_bulk_season_all(session_client, user):
    show, season = _build_show_in_db()
    season_row = Season.objects.create(user=user, item=season)
    resp = session_client.post(
        f"/api/v1/tracking/season/{season_row.pk}/bulk",
        data=json.dumps({"action": "all"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert Episode.objects.filter(user=user, status=Status.COMPLETED.value).count() == 3


# -------------------------------------------------------------------- history


def test_history_requires_bearer_token():
    response = Client(enforce_csrf_checks=True).post(
        "/api/v1/history",
        data=json.dumps({
            "from": "2026-08-07T12:00:00Z",
            "to": "2026-08-08T12:00:00Z",
        }),
        content_type="application/json",
    )
    assert response.status_code == 401


def test_history_returns_bearer_users_watches_in_range(user):
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="42", title="A Show",
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="42",
        season_number=1, parent=show, title="Season 1",
    )
    episode = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="42",
        season_number=1, episode_number=1, parent=season, title="Pilot", runtime=22,
    )
    movie = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1",
        title="A Movie", runtime=136,
    )
    book = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value, media_id="OL1W", title="A Book",
    )
    start = datetime(2026, 8, 7, 12, tzinfo=UTC)
    movie_event = CompletionEvent.objects.create(
        user=user, item=movie, completed_at=start, precision="instant",
    )
    episode_event = CompletionEvent.objects.create(
        user=user, item=episode, completed_at=start + timedelta(hours=6), precision="instant",
    )
    CompletionEvent.objects.create(
        user=user, item=book, completed_at=start + timedelta(hours=7), precision="instant",
    )
    CompletionEvent.objects.create(
        user=user, item=movie, completed_at=start + timedelta(days=1), precision="instant",
    )
    _, raw = APIToken.mint(user, ttl="1d")

    response = Client(enforce_csrf_checks=True).post(
        "/api/v1/history",
        data=json.dumps({
            "from": "2026-08-07T12:00:00Z",
            "to": "2026-08-08T12:00:00Z",
        }),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": f"watch-{movie_event.pk}",
                "watched_at": "2026-08-07T12:00:00Z",
                "type": "movie",
                "title": "A Movie",
                "runtime_minutes": 136,
            },
            {
                "id": f"watch-{episode_event.pk}",
                "watched_at": "2026-08-07T18:00:00Z",
                "type": "episode",
                "title": "A Show — Pilot",
                "runtime_minutes": 22,
            },
        ]
    }


# ---------------------------------------------------------------------- stats


def test_stats_endpoint(session_client, user):
    resp = session_client.get("/api/v1/me/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["movies_completed"] == 0
    assert body["pages_read"] == 0


def test_up_next_returns_movies_and_books(session_client, user):
    """Plan: Up Next is for shows, movies, and books — not just shows."""
    from core.enums import MediaType, Status
    book_item = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value,
        media_id="OL1W", title="A Book", max_progress=300,
    )
    from tracking.models import Book
    Book.objects.create(
        user=user, item=book_item, status=Status.IN_PROGRESS.value, progress=42,
    )

    movie_item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="100",
        title="Half-watched movie",
    )
    Movie.objects.create(
        user=user, item=movie_item, status=Status.IN_PROGRESS.value, progress=0,
    )

    body = session_client.get("/api/v1/me/up-next").json()
    assert "shows" in body and "movies" in body and "books" in body
    assert {b["title"] for b in body["books"]} == {"A Book"}
    assert body["books"][0]["progress"] == 42
    assert {m["title"] for m in body["movies"]} == {"Half-watched movie"}


# ------------------------------------------------------------------- exports


def test_export_json(session_client, user):
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1",
        title="M",
    )
    Movie.objects.create(user=user, item=item, status=Status.COMPLETED.value)
    resp = session_client.get("/api/v1/me/export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["movies"][0]["title"] == "M"
    assert body["movies"][0]["status"] == "completed"


def test_export_csv_movies_shows(session_client, user):
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="9",
        title="MovieZ",
    )
    Movie.objects.create(user=user, item=item, status=Status.COMPLETED.value, score=8)
    resp = session_client.get("/api/v1/exports/csv", {"type": "movies_shows"})
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    assert b"MovieZ" in resp.content
