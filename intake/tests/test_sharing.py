"""Static history sharing: private link management and offline rendering."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core.models import AppSettings, Item
from core.settings_store import history_sync_interval
from sharing.models import AccessLink
from sharing.static_history import _history_context, _monday, _poster_asset, render_static_history
from tracking.models import TV, CompletionEvent, Movie

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


def test_link_active_lifecycle(user):
    link = AccessLink.objects.create(owner=user)
    assert link.scope == "history"
    assert link.is_active()

    link.expires_at = timezone.now() - timedelta(seconds=1)
    link.save()
    assert not link.is_active()

    link.expires_at = None
    link.revoked_at = timezone.now()
    link.save()
    assert not link.is_active()


@override_settings(INTAKE_HISTORY_PUBLIC_URL="https://intake.example")
def test_manage_requires_login_and_displays_static_url(client, user):
    AccessLink.objects.create(owner=user, scope="history", token="opaque-token")

    response = client.get(reverse("sharing:manage"))

    assert response.status_code == 200
    assert "https://intake.example/s/opaque-token/" in response.content.decode()
    anon = Client().get(reverse("sharing:manage"))
    assert anon.status_code == 302


@override_settings(INTAKE_HISTORY_PUBLIC_URL="https://environment.example")
def test_manage_uses_editable_public_domain(client, user):
    settings_row = AppSettings.load()
    settings_row.history_public_url = "https://history.example"
    settings_row.save()
    AccessLink.objects.create(owner=user, scope="history", token="opaque-token")

    response = client.get(reverse("sharing:manage"))

    content = response.content.decode()
    assert "https://history.example/s/opaque-token/" in content
    assert "https://environment.example/s/opaque-token/" not in content


def test_save_public_domain_accepts_bare_domain(client):
    response = client.post(
        reverse("sharing:save_origin"),
        {"history_public_url": "history.example/"},
    )

    assert response.status_code == 302
    assert AppSettings.load().history_public_url == "https://history.example"


@pytest.mark.parametrize(
    "value",
    ["http://history.example", "https://history.example/path", "not a domain"],
)
def test_save_public_domain_rejects_unsafe_origins(client, value):
    response = client.post(
        reverse("sharing:save_origin"),
        {"history_public_url": value},
    )

    assert response.status_code == 302
    assert AppSettings.load().history_public_url == ""


@pytest.mark.parametrize(
    ("value", "unit", "seconds"),
    [("15", "minutes", 900), ("6", "hours", 21600), ("1", "hours", 3600)],
)
def test_save_history_sync_interval(client, value, unit, seconds):
    response = client.post(
        reverse("sharing:save_sync_interval"),
        {"sync_value": value, "sync_unit": unit},
    )

    assert response.status_code == 302
    settings_row = AppSettings.load()
    assert settings_row.history_sync_interval_value == int(value)
    assert settings_row.history_sync_interval_unit == unit
    assert history_sync_interval()[2] == seconds


@pytest.mark.parametrize(
    ("value", "unit"),
    [("0", "hours"), ("-1", "minutes"), ("abc", "hours"), ("6", "days")],
)
def test_save_history_sync_interval_rejects_invalid_values(client, value, unit):
    response = client.post(
        reverse("sharing:save_sync_interval"),
        {"sync_value": value, "sync_unit": unit},
    )

    assert response.status_code == 302
    settings_row = AppSettings.load()
    assert settings_row.history_sync_interval_value == 12
    assert settings_row.history_sync_interval_unit == "hours"


def test_create_only_creates_history_links(client, user):
    response = client.post(
        reverse("sharing:create"),
        {"label": "Friends", "ttl": "never"},
    )

    assert response.status_code == 302
    link = AccessLink.objects.get(owner=user)
    assert link.scope == "history"
    assert link.label == "Friends"
    assert link.expires_at is None


def test_create_rejects_unknown_expiry(client):
    response = client.post(reverse("sharing:create"), {"ttl": "century"})
    assert response.status_code == 400


def test_revoke_is_owner_scoped(client, user):
    link = AccessLink.objects.create(owner=user, scope="history")

    response = client.post(reverse("sharing:revoke", args=[link.pk]))

    assert response.status_code == 302
    link.refresh_from_db()
    assert link.revoked_at is not None


def _event(user, *, title="A Movie", watched_at=None):
    item = Item.objects.create(
        source="tmdb",
        media_type="movie",
        media_id=title.lower().replace(" ", "-"),
        title=title,
        runtime=120,
        metadata_complete=True,
    )
    return CompletionEvent.objects.create(
        user=user,
        item=item,
        completed_at=watched_at or timezone.now(),
    )


def test_renderer_builds_blank_root_and_token_history(tmp_path: Path, user):
    link = AccessLink.objects.create(
        owner=user,
        scope="history",
        label="Public watches",
        token="opaque-token",
    )
    _event(user, watched_at=timezone.now())

    result = render_static_history(tmp_path / "site")

    assert result["links"] == 1
    root = (tmp_path / "site" / "index.html").read_text()
    assert "<body></body>" in root
    private_root = tmp_path / "site" / "__shares" / link.token
    page = (private_root / "index.html").read_text()
    assert "Public watches" in page
    assert "A Movie" in page
    assert f'datetime="{timezone.localdate():%Y-%m-%d}"' in page
    assert 'aria-label="Expanded 30-day history"' in page
    assert "Week rhythm" not in page
    assert "30-day signal" not in page
    assert 'href="/history.css"' in page
    assert "<script" not in page
    assert 'href="/history/' not in page
    assert f"/s/{link.token}/" not in page
    assert (private_root / "meta.json").is_file()
    assert not (tmp_path / "site" / "history.js").exists()
    assert (tmp_path / "site" / "_worker.js").is_file()
    assert 'href="http' not in page
    assert 'src="http' not in page


def test_renderer_omits_revoked_expired_and_old_scope_links(tmp_path: Path, user):
    AccessLink.objects.create(
        owner=user,
        scope="history",
        token="revoked",
        revoked_at=timezone.now(),
    )
    AccessLink.objects.create(
        owner=user,
        scope="history",
        token="expired",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    AccessLink.objects.create(owner=user, scope="all", token="old-library-share")

    result = render_static_history(tmp_path / "site")

    assert result == {"links": 0, "pages": 0}
    assert not (tmp_path / "site" / "__shares").exists()


def test_renderer_replaces_output_so_revocations_remove_directories(tmp_path: Path, user):
    link = AccessLink.objects.create(owner=user, scope="history", token="remove-me")
    output = tmp_path / "site"
    render_static_history(output)
    assert (output / "__shares" / link.token / "index.html").exists()

    link.revoked_at = timezone.now()
    link.save()
    render_static_history(output)

    assert not (output / "__shares" / link.token).exists()


def test_expiring_link_metadata_is_stable_between_renders(tmp_path: Path, user):
    expires_at = timezone.now() + timedelta(days=7)
    link = AccessLink.objects.create(
        owner=user,
        scope="history",
        token="stable-expiry",
        expires_at=expires_at,
    )
    output = tmp_path / "site"

    render_static_history(output)
    metadata_path = output / "__shares" / link.token / "meta.json"
    first = metadata_path.read_text()
    render_static_history(output)
    second = metadata_path.read_text()

    assert first == second
    assert json.loads(first)["expires_at"] == int(expires_at.timestamp())


def test_renderer_exposes_only_a_rolling_30_days(tmp_path: Path, user):
    link = AccessLink.objects.create(owner=user, scope="history", token="windowed")
    _event(user, title="Recent Movie", watched_at=timezone.now() - timedelta(days=2))
    _event(user, title="Old Movie", watched_at=timezone.now() - timedelta(days=31))

    result = render_static_history(tmp_path / "site")

    assert result["pages"] == 4
    pages = (tmp_path / "site" / "__shares" / link.token).glob("**/*.html")
    rendered = "".join(page.read_text() for page in pages)
    assert "Recent Movie" in rendered
    assert "Old Movie" not in rendered


def test_timeline_groups_episodes_per_day_and_preserves_gaps(user):
    link = AccessLink.objects.create(owner=user)
    monday = _monday(timezone.localdate())
    previous_monday = monday - timedelta(days=7)
    watched_at = timezone.make_aware(
        datetime.combine(previous_monday, time.min)
    ) + timedelta(hours=12)
    show = Item.objects.create(source="tmdb", media_type="tv", media_id="timeline", title="A Show")
    season = Item.objects.create(
        source="tmdb", media_type="season", media_id="timeline",
        title="Season 1", parent=show, season_number=1,
    )
    events = []
    for number, day_offset in [(1, 0), (2, 0), (4, 0), (5, 1)]:
        item = Item.objects.create(
            source="tmdb", media_type="episode", media_id="timeline",
            title=f"Episode {number}", parent=season, season_number=1, episode_number=number,
        )
        events.append(CompletionEvent.objects.create(
            user=user, item=item, completed_at=watched_at + timedelta(days=day_offset),
        ))
    context = _history_context(
        link, list(reversed(events)), previous_monday, monday, {},
    )
    active_days = [day for day in context["days"] if day["entry_count"]]
    assert [day["date"] for day in active_days] == [
        previous_monday, previous_monday + timedelta(days=1),
    ]
    assert active_days[0]["watches"][0]["episode_code"] == "S1 · E1–2, E4"
    assert active_days[1]["watches"][0]["episode_code"] == "S1 · E5"
    assert active_days[0]["watches"][0]["completion_count"] == 3


def test_posters_are_bundled_and_cached_without_repeated_requests(tmp_path, monkeypatch):
    calls = []

    @contextmanager
    def stream(method, url, **kwargs):
        calls.append(url)
        yield httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=b"poster",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("sharing.static_history.httpx.stream", stream)
    cache, output = tmp_path / "cache", tmp_path / "site"
    url = "https://image.tmdb.org/t/p/w342/test.jpg"
    asset = _poster_asset(url, cache, output)
    assert asset.startswith("/posters/")
    assert (output / asset.lstrip("/")).read_bytes() == b"poster"
    assert _poster_asset(url, cache, output) == asset
    assert calls == [url]
    assert _poster_asset("https://untrusted.example/test.jpg", cache, output) == ""
    assert calls == [url]


def test_oversized_posters_fall_back_without_writing_assets(tmp_path, monkeypatch):
    @contextmanager
    def stream(method, url, **kwargs):
        yield httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=b"oversized",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("sharing.static_history.httpx.stream", stream)
    monkeypatch.setattr("sharing.static_history.POSTER_LIMIT", 4)
    cache, output = tmp_path / "cache", tmp_path / "site"
    assert _poster_asset("https://image.tmdb.org/test.jpg", cache, output) == ""
    assert not list(cache.iterdir())
    assert not (output / "posters").exists()


def test_public_cards_link_only_to_equivalent_tmdb_movies_and_shows(tmp_path, user):
    link = AccessLink.objects.create(owner=user, token="tmdb-links")
    movie = Item.objects.create(
        source="tmdb", media_type="movie", media_id="550", title="A TMDB movie",
    )
    show = Item.objects.create(
        source="tmdb", media_type="tv", media_id="1399", title="A TMDB show",
    )
    season = Item.objects.create(
        source="tmdb", media_type="season", media_id="1399", title="Season 1",
        parent=show, season_number=1,
    )
    episode = Item.objects.create(
        source="tmdb", media_type="episode", media_id="1399", title="Episode 1",
        parent=season, season_number=1, episode_number=1,
    )
    manual = Item.objects.create(
        source="manual", media_type="movie", media_id="42", title="Manual movie",
    )
    invalid = Item.objects.create(
        source="tmdb", media_type="movie", media_id="bad/path", title="Invalid ID",
    )
    for item in [movie, episode, manual, invalid]:
        CompletionEvent.objects.create(user=user, item=item, completed_at=timezone.now())
    render_static_history(tmp_path / "site")
    page = (tmp_path / "site" / "__shares" / link.token / "index.html").read_text()
    assert 'href="https://www.themoviedb.org/movie/550"' in page
    assert 'href="https://www.themoviedb.org/tv/1399"' in page
    assert page.count('rel="noopener noreferrer"') == 2
    assert page.count('target="_blank"') == 2
    assert "https://www.themoviedb.org/movie/42" not in page
    assert "https://www.themoviedb.org/movie/bad/path" not in page
    assert 'href="/1399/' not in page


def test_static_watchlist_matches_planning_rows_and_filters(tmp_path, user):
    link = AccessLink.objects.create(owner=user, token="watchlist-share")
    for media_type, model, media_id, title, status in [
        ("movie", Movie, "550", "Planned Movie", "planning"),
        ("tv", TV, "1399", "Planned Show", "planning"),
        ("movie", Movie, "551", "Watched Movie", "completed"),
    ]:
        item = Item.objects.create(
            source="tmdb", media_type=media_type, media_id=media_id, title=title,
        )
        model.objects.create(user=user, item=item, status=status)
    other = get_user_model().objects.create_user(username="other-watchlist")
    private_item = Item.objects.create(
        source="tmdb", media_type="movie", media_id="552", title="Other user secret",
    )
    Movie.objects.create(user=other, item=private_item, status="planning")
    render_static_history(tmp_path / "site")
    private = tmp_path / "site" / "__shares" / link.token
    html = (private / "watchlist" / "index.html").read_text()
    assert "Planned Movie" in html and "Planned Show" in html
    assert "Watched Movie" not in html and "Other user secret" not in html
    assert 'href="https://www.themoviedb.org/movie/550"' in html
    assert 'href="https://www.themoviedb.org/tv/1399"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "hx-post" not in html and "<form" not in html and "<script" not in html
    movies = (private / "watchlist" / "movie" / "index.html").read_text()
    shows = (private / "watchlist" / "tv" / "index.html").read_text()
    assert "Planned Movie" in movies and "Planned Show" not in movies
    assert "Planned Show" in shows and "Planned Movie" not in shows
    assert 'href="/watchlist/"' in (private / "index.html").read_text()


def test_no_public_django_share_route():
    with pytest.raises(NoReverseMatch):
        reverse("sharing:public", args=["token"])
