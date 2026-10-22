"""Tracking service layer: upsert idempotency + season fan-out."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from core.enums import MediaType, Status
from core.models import Item
from providers.base import EpisodeMetadata, Metadata, SeasonMetadata
from tracking import services
from tracking.models import CompletionEvent, Movie

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


def _movie_meta(media_id="27205", title="Inception") -> Metadata:
    return Metadata(
        source="tmdb",
        media_type=MediaType.MOVIE.value,
        media_id=media_id,
        title=title,
        max_progress=1,
        raw={"id": int(media_id)},
    )


def test_upsert_item_creates_then_updates():
    item1 = services.upsert_item(_movie_meta(title="v1"))
    item2 = services.upsert_item(_movie_meta(title="v2"))

    assert item1.pk == item2.pk
    assert Item.objects.count() == 1
    item2.refresh_from_db()
    assert item2.title == "v2"


def test_upsert_strips_numbers_for_root_types():
    item = services.upsert_item(_movie_meta())
    assert item.season_number is None
    assert item.episode_number is None


def test_upsert_season_with_episodes_wires_parent():
    tv = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="1399", title="GoT"
    )
    season_payload = SeasonMetadata(
        source="tmdb",
        media_type=MediaType.SEASON.value,
        media_id="1399",
        title="Season 1",
        season_number=1,
        episodes=[
            EpisodeMetadata(
                source="tmdb",
                media_type=MediaType.EPISODE.value,
                media_id="1399",
                title="Pilot",
                season_number=1,
                episode_number=1,
            ),
            EpisodeMetadata(
                source="tmdb",
                media_type=MediaType.EPISODE.value,
                media_id="1399",
                title="Ep2",
                season_number=1,
                episode_number=2,
            ),
        ],
    )

    season = services.upsert_season_with_episodes(season_payload, tv_item=tv)
    assert season.parent_id == tv.pk
    assert season.season_number == 1
    episodes = Item.objects.filter(parent=season).order_by("episode_number")
    assert episodes.count() == 2
    assert all(e.season_number == 1 for e in episodes)
    assert [e.episode_number for e in episodes] == [1, 2]

    # Re-running is idempotent — no duplicates.
    services.upsert_season_with_episodes(season_payload, tv_item=tv)
    assert Item.objects.filter(parent=season).count() == 2


def test_update_tracking_creates_then_updates(user):
    item = services.upsert_item(_movie_meta())
    row1 = services.update_tracking(user=user, item=item, score=8, status=Status.COMPLETED.value)
    assert isinstance(row1, Movie)
    assert row1.score == 8
    assert row1.status == Status.COMPLETED.value
    assert Movie.objects.count() == 1

    row2 = services.update_tracking(user=user, item=item, score=9)
    assert row2.pk == row1.pk
    assert row2.score == 9


def test_mark_watched_sets_progress_and_completed(user):
    item = services.upsert_item(_movie_meta())
    row = services.mark_watched(user=user, item=item)
    assert row.status == Status.COMPLETED.value
    assert row.progress == 1


def test_record_completion_rolls_back_event_when_tracking_save_fails(
    user, monkeypatch,
):
    item = services.upsert_item(_movie_meta())
    row = Movie.objects.create(user=user, item=item)
    original_save = Movie.save

    def fail_completed_save(self, *args, **kwargs):
        if self.status == Status.COMPLETED.value:
            raise RuntimeError("tracking save failed")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Movie, "save", fail_completed_save)

    with pytest.raises(RuntimeError, match="tracking save failed"):
        services.record_completion(user=user, item=item)

    assert not CompletionEvent.objects.filter(user=user, item=item).exists()
    row.refresh_from_db()
    assert row.status == Status.PLANNING.value
