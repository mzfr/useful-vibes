"""Up Next feed selection logic."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import CompletionEvent, Episode
from tracking.up_next import shows_for_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


def _build_show(media_id="9", episodes=2, future_eps=0):
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id=media_id, title=f"Show {media_id}"
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id=media_id,
        season_number=1, parent=show, title="S1",
    )
    today = date.today()
    eps = []
    for n in range(1, episodes + 1):
        eps.append(Item.objects.create(
            source="tmdb", media_type=MediaType.EPISODE.value, media_id=media_id,
            season_number=1, episode_number=n, parent=season, title=f"E{n}",
            release_date=today - timedelta(days=10 - n),
        ))
    for n in range(episodes + 1, episodes + future_eps + 1):
        eps.append(Item.objects.create(
            source="tmdb", media_type=MediaType.EPISODE.value, media_id=media_id,
            season_number=1, episode_number=n, parent=season, title=f"E{n}",
            release_date=today + timedelta(days=30),
        ))
    return show, season, eps


def test_no_engagement_returns_empty(user):
    _build_show()
    assert shows_for_user(user) == []


def test_mark_next_watched_records_click_time(client, user):
    _show, _season, episodes = _build_show()
    client.force_login(user)

    before = timezone.now()
    response = client.post(
        reverse("tracking:up_next_mark_watched", args=[episodes[0].pk])
    )
    after = timezone.now()

    assert response.status_code == 200
    event = CompletionEvent.objects.get(user=user, item=episodes[0])
    assert event.precision == "instant"
    assert before <= event.completed_at <= after


def test_picks_next_unwatched_episode(user):
    show, _season, eps = _build_show(episodes=4)
    # Watched first two
    for ep in eps[:2]:
        Episode.objects.create(
            user=user, item=ep, status=Status.COMPLETED.value,
            progressed_at=timezone.now(),
        )
    rows = shows_for_user(user)
    assert len(rows) == 1
    assert rows[0].next_episode.episode_number == 3


def test_skips_unaired_episodes(user):
    show, _season, eps = _build_show(episodes=2, future_eps=1)
    for ep in eps[:2]:
        Episode.objects.create(
            user=user, item=ep, status=Status.COMPLETED.value,
            progressed_at=timezone.now(),
        )
    # Only E3 left, which airs in the future → no row.
    assert shows_for_user(user) == []


def test_orders_by_most_recent_progress(user):
    s1, _, eps1 = _build_show(media_id="100", episodes=3)
    s2, _, eps2 = _build_show(media_id="200", episodes=3)
    now = timezone.now()
    Episode.objects.create(user=user, item=eps1[0], status=Status.COMPLETED.value,
                           progressed_at=now - timedelta(days=5))
    Episode.objects.create(user=user, item=eps2[0], status=Status.COMPLETED.value,
                           progressed_at=now - timedelta(days=1))

    rows = shows_for_user(user)
    assert [r.show_item.media_id for r in rows] == ["200", "100"]


def test_show_drops_off_when_complete(user):
    show, _season, eps = _build_show(episodes=2)
    for ep in eps:
        Episode.objects.create(
            user=user, item=ep, status=Status.COMPLETED.value,
            progressed_at=timezone.now(),
        )
    assert shows_for_user(user) == []
