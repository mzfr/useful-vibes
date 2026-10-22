"""Regression tests for the third advisor-review round."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from releases.models import ReleaseCheck
from releases.services import _diff_for_user, build_feed
from tracking.models import Episode, Movie
from tracking.up_next import shows_for_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


def _show_tree(media_id="55", n_eps=3):
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id=media_id, title="Show"
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id=media_id,
        season_number=1, parent=show, title="S1",
    )
    eps = [
        Item.objects.create(
            source="tmdb", media_type=MediaType.EPISODE.value, media_id=media_id,
            season_number=1, episode_number=n, parent=season, title=f"E{n}",
            release_date=date.today() - timedelta(days=30),
        )
        for n in range(1, n_eps + 1)
    ]
    return show, season, eps


# ------------------------- P2: Up Next must not crash on progressed_at=None


def test_up_next_survives_null_progressed_at(user):
    """Two shows, one with progressed_at=None — sort must not TypeError."""
    s1, _, eps1 = _show_tree(media_id="100")
    s2, _, eps2 = _show_tree(media_id="200")
    Episode.objects.create(
        user=user, item=eps1[0], status=Status.COMPLETED.value,
        progressed_at=timezone.now(),
    )
    # NULL progressed_at (admin edit, history restore, bump_progressed_at=False)
    Episode.objects.create(
        user=user, item=eps2[0], status=Status.COMPLETED.value,
        progressed_at=None,
    )

    rows = shows_for_user(user)
    assert len(rows) == 2
    # The show with real activity sorts first; the NULL one sinks to the bottom.
    assert rows[0].show_item.media_id == "100"


# ------------------------- P2: mark_watched_again stamps progressed_at


def test_mark_watched_again_bumps_progressed_at(user):
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1",
        title="M", max_progress=1,
    )
    row = Movie.objects.create(user=user, item=item, status=Status.COMPLETED.value)
    assert row.progressed_at is None

    row.mark_watched_again()
    row.refresh_from_db()
    assert row.progressed_at is not None
    assert row.repeats == 1


# ------------------------- P2: releases read-time diff + badge re-arm


PAYLOAD = {
    "seasons": [
        {"season_number": 1, "air_date": "2020-01-01", "episode_count": 3},
        {"season_number": 2, "air_date": "2021-01-01", "episode_count": 3},
    ],
    "episodes": {
        "1": [
            {"episode_number": 1, "title": "E1", "air_date": "2020-01-01"},
            {"episode_number": 2, "title": "E2", "air_date": "2020-01-08"},
        ],
    },
}


def test_releases_diff_applies_current_watched_state(user):
    """Watching an episode removes it from the diff immediately — no TTL wait."""
    show, season, eps = _show_tree(media_id="77", n_eps=2)
    # Engage with season 1 by watching E1.
    Episode.objects.create(
        user=user, item=eps[0], status=Status.COMPLETED.value,
        progressed_at=timezone.now(),
    )

    seasons, episodes = _diff_for_user(show, PAYLOAD, engaged={1}, watched={(1, 1)})
    # Season 2 is new (not engaged), season 1 is engaged so not "new".
    assert [s.season_number for s in seasons] == [2]
    # E2 unwatched → listed. E1 watched → excluded.
    assert [(e.season_number, e.episode_number) for e in episodes] == [(1, 2)]

    # Watch E2 → the diff updates instantly from the same cached payload.
    Episode.objects.create(
        user=user, item=eps[1], status=Status.COMPLETED.value,
        progressed_at=timezone.now(),
    )
    from releases.services import _show_engagement
    engaged_map, watched_map = _show_engagement(user, [show.pk])
    _, episodes = _diff_for_user(
        show, PAYLOAD, engaged_map.get(show.pk, set()), watched_map.get(show.pk, set())
    )
    assert episodes == []


def test_releases_badge_rearms_on_new_content(user, monkeypatch):
    """seen_at resets when a refresh produces different payload."""
    show, _, eps = _show_tree(media_id="88", n_eps=1)
    Episode.objects.create(
        user=user, item=eps[0], status=Status.COMPLETED.value,
        progressed_at=timezone.now(),
    )

    payload_v1 = {"seasons": [], "episodes": {}}
    payload_v2 = {
        "seasons": [{"season_number": 2, "air_date": "2021-01-01", "episode_count": 3}],
        "episodes": {},
    }

    calls = {"n": 0}

    def fake_fetch(user_, show_):
        calls["n"] += 1
        return payload_v1 if calls["n"] == 1 else payload_v2

    monkeypatch.setattr("releases.services._fetch_provider_payload", fake_fetch)

    # First build: empty payload, user acks.
    build_feed(user)
    from releases.services import mark_all_seen
    mark_all_seen(user)
    cache = ReleaseCheck.objects.get(user=user, item=show)
    assert cache.seen_at is not None

    # Force staleness, refresh finds season 2 → badge re-arms.
    ReleaseCheck.objects.filter(pk=cache.pk).update(
        last_checked_at=timezone.now() - timedelta(hours=25)
    )
    feed = build_feed(user)
    cache.refresh_from_db()
    assert cache.seen_at is None
    assert feed["unread_count"] == 1
    assert [s.season_number for s in feed["new_seasons"]] == [2]


# ------------------------- P3: OL date parsing


def test_openlibrary_year_extraction():
    from providers.openlibrary import _first_parseable_date, _year_to_date

    assert _year_to_date("May 1, 1999").year == 1999
    assert _year_to_date("1954").year == 1954
    assert _year_to_date(2005).year == 2005
    assert _year_to_date("no year here") is None
    assert _year_to_date(None) is None

    # Prose edition date fails → falls through to the work's clean year.
    assert _first_parseable_date("not-a-date", "1954").year == 1954
    # First parseable wins.
    assert _first_parseable_date("May 1, 1999", "1954").year == 1999
    assert _first_parseable_date(None, None) is None


# ------------------------- P2: prod refuses wildcard ALLOWED_HOSTS


def test_prod_settings_reject_wildcard_hosts(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("INTAKE_SECRET_KEY", "x")
    monkeypatch.setenv("INTAKE_ALLOWED_HOSTS", "*")
    sys.modules.pop("config.settings.prod", None)
    with pytest.raises(RuntimeError, match="must not contain"):
        importlib.import_module("config.settings.prod")
    sys.modules.pop("config.settings.prod", None)


def test_prod_settings_require_allowed_hosts(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("INTAKE_SECRET_KEY", "x")
    monkeypatch.delenv("INTAKE_ALLOWED_HOSTS", raising=False)
    sys.modules.pop("config.settings.prod", None)
    with pytest.raises(RuntimeError, match="INTAKE_ALLOWED_HOSTS"):
        importlib.import_module("config.settings.prod")
    sys.modules.pop("config.settings.prod", None)


def test_prod_settings_accept_host_list(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("INTAKE_SECRET_KEY", "x")
    monkeypatch.setenv("INTAKE_ALLOWED_HOSTS", "localhost, 192.168.1.10")
    sys.modules.pop("config.settings.prod", None)
    mod = importlib.import_module("config.settings.prod")
    assert mod.ALLOWED_HOSTS == ["localhost", "192.168.1.10"]
    sys.modules.pop("config.settings.prod", None)
