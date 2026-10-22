"""Query-count regression guards.

These pin the pages that previously issued one query per show / one per row.
The dataset is grown and the query budget must stay effectively constant —
this is what prevents the N+1s from creeping back in.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.enums import MediaType, Status
from core.models import Item
from stats.services import build_stats
from tracking.models import CompletionEvent, Episode
from tracking.up_next import shows_for_user

pytestmark = pytest.mark.django_db


def _build_library(user, n_shows: int, eps_per_season: int = 3, seasons: int = 2, prefix="a"):
    """Create n_shows, each with `seasons` seasons of `eps_per_season` episodes,
    and watch one episode per show so it shows up in Up Next."""
    base = date.today() - timedelta(days=60)
    for s in range(n_shows):
        show = Item.objects.create(
            source="tmdb", media_type=MediaType.TV.value, media_id=f"{prefix}-show-{s}",
            title=f"{prefix} Show {s}",
        )
        for sn in range(1, seasons + 1):
            season = Item.objects.create(
                source="tmdb", media_type=MediaType.SEASON.value, media_id=f"{prefix}-show-{s}",
                season_number=sn, parent=show, title=f"S{sn}",
            )
            for en in range(1, eps_per_season + 1):
                ep = Item.objects.create(
                    source="tmdb", media_type=MediaType.EPISODE.value,
                    media_id=f"{prefix}-show-{s}", season_number=sn, episode_number=en,
                    parent=season, title=f"E{en}", runtime=45,
                    release_date=base + timedelta(days=(sn - 1) * 10 + en),
                )
                if en == 1:
                    # Watch the first episode so the show is engaged + has a next.
                    Episode.objects.create(
                        user=user, item=ep, status=Status.COMPLETED.value,
                        progressed_at=base + timedelta(days=sn),
                    )
        # One movie per show to exercise stats aggregation too.
        movie = Item.objects.create(
            source="tmdb", media_type=MediaType.MOVIE.value, media_id=f"{prefix}-movie-{s}",
            title=f"{prefix} Movie {s}", runtime=100, max_progress=1,
            release_date=base, raw={"genres": [{"name": "Drama"}]},
        )
        CompletionEvent.objects.create(
            user=user, item=movie, source="manual", precision="instant",
            completed_at=base + timedelta(hours=12),
        )


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="perf")


def _count(fn, *args):
    with CaptureQueriesContext(connection) as ctx:
        fn(*args)
    return len(ctx.captured_queries)


def test_up_next_query_count_is_bounded(user):
    """Up Next must not issue a query per show, no matter the library size."""
    _build_library(user, n_shows=5, prefix="small")
    small = _count(shows_for_user, user)

    _build_library(user, n_shows=25, prefix="big")  # 30 shows total
    big = _count(shows_for_user, user)

    # Budget: a handful of batched queries. Both sizes land in the same band.
    assert small <= 12, f"Up Next small library issued {small} queries"
    assert big <= 12, f"Up Next 30-show library issued {big} queries"


def test_stats_query_count_is_bounded(user):
    """Stats must stay roughly constant as the event catalog grows."""
    _build_library(user, n_shows=5, prefix="small")
    small = _count(build_stats, user)

    _build_library(user, n_shows=25, prefix="big")
    big = _count(build_stats, user)

    # Stats is a fixed set of aggregate/batched queries; allow a small cushion.
    assert small <= 14, f"Stats small library issued {small} queries"
    assert big <= 14, f"Stats large library issued {big} queries"
