"""Season bulk endpoints (all/range) + per-episode watch/unwatch/rate."""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import TV, CompletionEvent, Episode

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def show_tree():
    """Build a tiny show/season/episode hierarchy directly via Item."""
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="42", title="Test Show"
    )
    season = Item.objects.create(
        source="tmdb",
        media_type=MediaType.SEASON.value,
        media_id="42",
        season_number=1,
        parent=show,
        title="Season 1",
    )
    today = date.today()
    e1 = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="42",
        season_number=1, episode_number=1, parent=season, title="Pilot",
        release_date=today - timedelta(days=30),
    )
    e2 = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="42",
        season_number=1, episode_number=2, parent=season, title="Ep2",
        release_date=today - timedelta(days=20),
    )
    e3 = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="42",
        season_number=1, episode_number=3, parent=season, title="Ep3",
        release_date=today + timedelta(days=10),   # not aired yet
    )
    return {"show": show, "season": season, "episodes": [e1, e2, e3]}


def test_watch_again_logs_a_rewatch(client, user, show_tree):
    """Clicking Watched twice means "watched again", not undo."""
    url = reverse("tracking:toggle_episode", args=["42", 1, 1])
    resp = client.post(url)
    assert resp.status_code == 200

    row = Episode.objects.get(user=user, item=show_tree["episodes"][0])
    assert row.status == Status.COMPLETED.value
    assert row.repeats == 0

    client.post(url)
    row.refresh_from_db()
    assert row.status == Status.COMPLETED.value
    assert row.repeats == 1


def test_explicit_watch_date_remains_date_only(client, user, show_tree):
    response = client.post(
        reverse("tracking:toggle_episode", args=["42", 1, 1]),
        {"watched_on": "2026-01-02"},
    )

    assert response.status_code == 200
    event = CompletionEvent.objects.get(user=user, item=show_tree["episodes"][0])
    assert event.precision == "date"
    assert timezone.localtime(event.completed_at).date() == date(2026, 1, 2)
    assert timezone.localtime(event.completed_at).time() == time(12)


def test_unwatch_episode_clears_watched_state(client, user, show_tree):
    client.post(reverse("tracking:toggle_episode", args=["42", 1, 1]))

    resp = client.post(reverse("tracking:unwatch_episode", args=["42", 1, 1]))

    assert resp.status_code == 200
    row = Episode.objects.get(user=user, item=show_tree["episodes"][0])
    assert row.status == Status.PLANNING.value
    assert row.end_date is None


def test_rate_episode(client, user, show_tree):
    url = reverse("tracking:rate_episode", args=["42", 1, 2])
    resp = client.post(url, {"score": "9"})
    assert resp.status_code == 200
    row = Episode.objects.get(user=user, item=show_tree["episodes"][1])
    assert row.score == 9

    # 0 clears
    client.post(url, {"score": "0"})
    row.refresh_from_db()
    assert row.score is None


def test_rate_invalid(client, show_tree):
    url = reverse("tracking:rate_episode", args=["42", 1, 2])
    assert client.post(url, {"score": "11"}).status_code == 400
    assert client.post(url, {"score": "abc"}).status_code == 400


def test_bulk_all_marks_everything(client, user, show_tree):
    url = reverse("tracking:bulk_season", args=["42", 1])
    before = timezone.now()
    resp = client.post(url, {"action": "all"})
    after = timezone.now()
    assert resp.status_code == 200
    assert Episode.objects.filter(user=user, status=Status.COMPLETED.value).count() == 3
    events = CompletionEvent.objects.filter(user=user)
    assert events.count() == 3
    assert all(event.precision == "instant" for event in events)
    assert all(before <= event.completed_at <= after for event in events)


def test_bulk_season_removes_started_show_from_watchlist(client, user, show_tree):
    TV.objects.create(user=user, item=show_tree["show"], status=Status.PLANNING.value)

    resp = client.post(reverse("tracking:bulk_season", args=["42", 1]), {"action": "all"})

    assert resp.status_code == 200
    assert TV.objects.get(user=user, item=show_tree["show"]).status == Status.IN_PROGRESS.value


def test_bulk_range(client, user, show_tree):
    url = reverse("tracking:bulk_season", args=["42", 1])
    resp = client.post(url, {"action": "range", "start": "2", "end": "3"})
    assert resp.status_code == 200
    # The response re-renders that season's body, including the range control.
    body = resp.content.decode()
    assert 'id="season-panel-body-1"' in body
    assert "Mark range" in body
    watched = set(
        Episode.objects.filter(user=user, status=Status.COMPLETED.value)
        .values_list("item__episode_number", flat=True)
    )
    assert watched == {2, 3}


def test_season_date_is_optional_by_default():
    body = render_to_string(
        "tracking/partials/_season_accordion.html",
        {"seasons": []},
    )
    date_input = body.split('id="watched-on"', 1)[1].split(">", 1)[0]
    assert "value=" not in date_input


def test_season_panel_offers_inline_range_action(client, show_tree):
    response = client.get(reverse("tracking:season_panel", args=["42", 1]))

    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="season-panel-body-1"' in body
    assert 'name="action" value="range"' in body
    assert "Mark range" in body


def test_bulk_range_does_not_rewatch_existing(client, user, show_tree):
    url = reverse("tracking:bulk_season", args=["42", 1])
    client.post(url, {"action": "range", "start": "1", "end": "1"})
    client.post(url, {"action": "range", "start": "1", "end": "3"})

    first = Episode.objects.get(user=user, item=show_tree["episodes"][0])
    assert first.repeats == 0
    assert Episode.objects.filter(user=user, status=Status.COMPLETED.value).count() == 3


def test_bulk_range_swaps_inverted_bounds(client, user, show_tree):
    url = reverse("tracking:bulk_season", args=["42", 1])
    client.post(url, {"action": "range", "start": "3", "end": "1"})
    watched = Episode.objects.filter(user=user, status=Status.COMPLETED.value).count()
    assert watched == 3


def test_bulk_actions_never_force_a_rewatch(client, user, show_tree):
    """Bulk actions only fill gaps; logging another watch stays per-episode."""
    url = reverse("tracking:bulk_season", args=["42", 1])
    client.post(url, {"action": "all"})
    client.post(url, {"action": "all"})

    for ep in show_tree["episodes"]:
        row = Episode.objects.get(user=user, item=ep)
        assert row.status == Status.COMPLETED.value
        assert row.repeats == 0


def test_bulk_unknown_action(client, show_tree):
    url = reverse("tracking:bulk_season", args=["42", 1])
    assert client.post(url, {"action": "nope"}).status_code == 400
    assert client.post(url, {"action": "rewatch_season"}).status_code == 400
