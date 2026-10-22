from __future__ import annotations

import json
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.enums import Status
from core.models import Item
from imports.archive import export_payload
from imports.models import TraktLegacy
from imports.trakt_export import _legacy_payload
from releases.services import _shows_tracked_by
from tracking.models import Episode, ShowFeedPreference
from tracking.services import set_show_visibility
from tracking.up_next import shows_for_user

pytestmark = pytest.mark.django_db


def tree(user):
    show = Item.objects.create(source="tmdb", media_type="tv", media_id="1", title="Show")
    season = Item.objects.create(source="tmdb", media_type="season", media_id="1", season_number=1, parent=show, title="S1")
    e1 = Item.objects.create(source="tmdb", media_type="episode", media_id="1", season_number=1, episode_number=1, parent=season, title="E1", release_date=date(2020, 1, 1))
    Item.objects.create(source="tmdb", media_type="episode", media_id="1", season_number=1, episode_number=2, parent=season, title="E2", release_date=date(2020, 1, 2))
    Episode.objects.create(user=user, item=e1, status=Status.COMPLETED.value, progressed_at=timezone.now())
    return show


def test_visibility_flags_are_independent_and_do_not_touch_history():
    user = get_user_model().objects.create_user("u")
    show = tree(user)
    episode = Episode.objects.get(user=user)
    history_count = episode.history.count()

    set_show_visibility(user=user, show=show, up_next=True)
    assert shows_for_user(user) == []
    assert show in _shows_tracked_by(user)

    set_show_visibility(user=user, show=show, up_next=False, releases=True)
    assert shows_for_user(user)
    assert show not in _shows_tracked_by(user)
    episode.refresh_from_db()
    assert episode.status == Status.COMPLETED.value
    assert episode.history.count() == history_count


def test_visibility_is_user_isolated():
    user = get_user_model().objects.create_user("u")
    other = get_user_model().objects.create_user("o")
    show = tree(user)
    set_show_visibility(user=user, show=show, up_next=True, releases=True)
    assert not ShowFeedPreference.objects.filter(user=other).exists()


def test_legacy_whitelist_excludes_sensitive_fields():
    payload = _legacy_payload(
        {"username": "u", "ids": {"trakt": 1}, "email": "secret@example.com"},
        {"user": {"dob": "1990-01-01"}, "account": {"timezone": "Asia/Kolkata", "token": "secret"}, "browsing": {"search": {"recent_queries": ["secret"]}}},
    )
    encoded = json.dumps(payload)
    assert "secret" not in encoded
    assert "1990-01-01" not in encoded
    assert payload["settings"]["timezone"] == "Asia/Kolkata"


def test_archive_contains_visibility_and_legacy():
    user = get_user_model().objects.create_user("u")
    show = tree(user)
    set_show_visibility(user=user, show=show, up_next=True)
    TraktLegacy.objects.create(user=user, metadata={"schema_version": 1, "profile": {"username": "u"}})
    payload = export_payload(user)
    assert payload["schema_version"] == 3
    assert payload["show_feed_preferences"][0]["media_id"] == "1"
    assert payload["trakt_legacy"]["profile"]["username"] == "u"
