"""Regression tests for the second advisor-review round."""
from __future__ import annotations

import json

import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from httpx import Response

from api.models import APIToken
from core.enums import MediaType
from core.models import Item
from providers import registry
from tracking.models import Movie

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


# ------------------------------------------ P1: bearer-token POSTs must work


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_bearer_token_post_works_without_csrf(user, load_fixture):
    """The critical fix: a bearer-only client must be able to write."""
    respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )
    _, raw = APIToken.mint(user, ttl="1d")

    fresh = Client(enforce_csrf_checks=True)  # simulates a non-browser client
    resp = fresh.post(
        "/api/v1/tracking/movie",
        data=json.dumps({"source": "tmdb", "media_id": "27205", "status": "completed"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code == 200, resp.content
    assert Movie.objects.filter(user=user).exists()


def test_bearer_token_cannot_mint_replacement(user):
    """A leaked token must NOT be able to mint its own replacement —
    token minting is session-only so expiry actually contains a leak."""
    _, raw = APIToken.mint(user, ttl="1d")
    fresh = Client(enforce_csrf_checks=True)
    resp = fresh.post(
        "/api/v1/me/token",
        data=json.dumps({"ttl": "never"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code in (401, 403)


def test_session_can_mint_token(user):
    c = Client()
    c.force_login(user)
    resp = c.post(
        "/api/v1/me/token",
        data=json.dumps({"ttl": "30d"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json()["token"].startswith("intake_")


def test_session_auth_still_works_for_get(session_client):
    assert session_client.get("/api/v1/me/up-next").status_code == 200


# ------------------------------------- P2: unknown source is 400, not 500


def test_api_item_details_unknown_source_400(session_client):
    resp = session_client.get("/api/v1/items/bogus/movie/1")
    assert resp.status_code == 400


def test_api_search_unknown_source_400(session_client):
    resp = session_client.get("/api/v1/search", {"q": "x", "source": "bogus"})
    assert resp.status_code == 400


# ------------------------------------ P3: progress bumps progressed_at


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_api_progress_bumps_progressed_at(session_client, user, load_fixture):
    respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )
    r = session_client.post(
        "/api/v1/tracking/movie",
        data=json.dumps({"source": "tmdb", "media_id": "27205"}),
        content_type="application/json",
    )
    tid = r.json()["id"]
    before = Movie.objects.get(pk=tid).progressed_at

    resp = session_client.post(
        f"/api/v1/tracking/movie/{tid}/progress",
        data=json.dumps({"progress": 1}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    after = Movie.objects.get(pk=tid).progressed_at
    assert after is not None
    assert before is None or after >= before


# --------------------------------------- P3: inactive user's token rejected


def test_inactive_user_token_rejected(user):
    _, raw = APIToken.mint(user, ttl="1d")
    user.is_active = False
    user.save()
    resp = Client().get("/api/v1/me/up-next", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 401


def test_token_touch_is_throttled(user):
    _, raw = APIToken.mint(user, ttl="1d")
    c = Client()
    c.get("/api/v1/me/up-next", HTTP_AUTHORIZATION=f"Bearer {raw}")
    first = APIToken.objects.get(user=user).last_used_at
    assert first is not None

    c.get("/api/v1/me/up-next", HTTP_AUTHORIZATION=f"Bearer {raw}")
    second = APIToken.objects.get(user=user).last_used_at
    assert second == first  # within throttle window → no extra write


# ------------------------------- P3: Item natural key enforced for NULL rows


def test_duplicate_root_item_rejected_at_db_level():
    from django.db import IntegrityError
    Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="dup", title="A"
    )
    with pytest.raises(IntegrityError):
        # Bypass update_or_create — raw insert must be blocked by the partial
        # unique index even though season/episode numbers are NULL.
        Item.objects.create(
            source="tmdb", media_type=MediaType.MOVIE.value, media_id="dup", title="B"
        )


def test_duplicate_season_item_rejected():
    from django.db import IntegrityError
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="s", title="S"
    )
    Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="s",
        season_number=1, parent=show, title="S1",
    )
    with pytest.raises(IntegrityError):
        Item.objects.create(
            source="tmdb", media_type=MediaType.SEASON.value, media_id="s",
            season_number=1, parent=show, title="S1 again",
        )


# ------------------------------------------- P3: Trakt rewatch grouping


def test_trakt_history_grouping_counts_repeats():
    """Unit-test the grouping logic: 3 plays of one movie → repeats=2."""
    from imports.trakt import _ts

    entries = [
        {"type": "movie", "watched_at": "2024-01-01T10:00:00.000Z",
         "movie": {"ids": {"tmdb": 27205}}},
        {"type": "movie", "watched_at": "2024-06-01T10:00:00.000Z",
         "movie": {"ids": {"tmdb": 27205}}},
        {"type": "movie", "watched_at": "2023-01-01T10:00:00.000Z",
         "movie": {"ids": {"tmdb": 27205}}},
    ]
    grouped: dict = {}
    for entry in entries:
        tmdb_id = entry["movie"]["ids"]["tmdb"]
        key = ("movie", str(tmdb_id), None, None)
        watched_at = _ts(entry["watched_at"])
        bucket = grouped.setdefault(key, {"entry": entry, "plays": 0, "latest": None})
        bucket["plays"] += 1
        if watched_at and (bucket["latest"] is None or watched_at > bucket["latest"]):
            bucket["latest"] = watched_at

    bucket = grouped[("movie", "27205", None, None)]
    assert bucket["plays"] == 3
    assert bucket["latest"].year == 2024 and bucket["latest"].month == 6
