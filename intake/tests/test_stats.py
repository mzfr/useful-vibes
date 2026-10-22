"""Stats aggregation."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from stats.services import build_stats
from tracking.models import Book, CompletionEvent, Episode, Movie

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


def test_empty_stats(user):
    s = build_stats(user)
    assert s.totals.movies_completed == 0
    assert s.totals.pages_read == 0
    assert s.top_rated == []


def test_basic_aggregation(user):
    m1 = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1",
        title="A", runtime=120, max_progress=1, release_date=date(2010, 1, 1),
        raw={"genres": [{"name": "Action"}]},
    )
    m2 = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="2",
        title="B", runtime=90, max_progress=1, release_date=date(2022, 1, 1),
        raw={"genres": [{"name": "Action"}, {"name": "Drama"}]},
    )
    b = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value, media_id="OLX",
        title="Book", max_progress=300,
    )

    m1_row = Movie.objects.create(user=user, item=m1, status=Status.COMPLETED.value, score=9)
    # Two rewatches → 3 plays total. Rewatches are events, not a bare counter.
    m1_row.mark_watched_again()
    m1_row.mark_watched_again()
    Movie.objects.create(user=user, item=m2, status=Status.COMPLETED.value, score=7)
    Book.objects.create(user=user, item=b, status=Status.COMPLETED.value, score=8)

    s = build_stats(user)
    assert s.totals.movies_completed == 4
    assert s.totals.books_finished == 1
    # m1 (120min, repeats=2 → 3 plays = 360) + m2 (90min, repeats=0 → 90) = 450
    assert s.totals.minutes_watched == 450
    assert s.totals.pages_read == 300

    titles = [r.item.title for r in s.top_rated]
    assert titles[0] == "A"     # score 9
    assert "B" in titles

    assert s.score_histogram[9] == 1
    assert s.score_histogram[7] == 1
    assert s.score_histogram[8] == 1

    # Rewatch leader is the movie with repeats=2
    assert s.rewatch_leaders[0].item.title == "A"

    # Top genres counted from item.raw
    genre_dict = dict(s.top_genres)
    assert genre_dict["Action"] == 2
    assert genre_dict["Drama"] == 1

    # Decades from release_date
    decade_dict = dict(s.decades)
    assert decade_dict[2010] == 1
    assert decade_dict[2020] == 1


def test_rewatches_multiply_minutes_and_pages(user):
    """A movie watched 3 times must count 3× its runtime, not once."""
    movie_item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="rw1",
        title="Rewatch Me", runtime=148, max_progress=1,
    )
    movie_row = Movie.objects.create(
        user=user, item=movie_item, status=Status.COMPLETED.value,
    )
    movie_row.mark_watched_again()
    movie_row.mark_watched_again()

    book_item = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value, media_id="rb1",
        title="Reread Me", max_progress=300,
    )
    book_row = Book.objects.create(
        user=user, item=book_item, status=Status.COMPLETED.value,
    )
    book_row.mark_watched_again()

    s = build_stats(user)
    # 148 × 3 (original + 2 rewatches) = 444 min
    assert s.totals.minutes_watched == 444
    # 300 × 2 (original + 1 reread) = 600 pages
    assert s.totals.pages_read == 600


def test_episodes_rewatched_count_full_runtime(user):
    """Each episode rewatch adds its runtime back."""
    from tracking.models import Episode
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="44", title="S",
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="44",
        season_number=1, parent=show, title="S1",
    )
    e1 = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="44",
        season_number=1, episode_number=1, parent=season, title="E1", runtime=60,
    )
    e2 = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="44",
        season_number=1, episode_number=2, parent=season, title="E2", runtime=60,
    )
    Episode.objects.create(user=user, item=e1, status=Status.COMPLETED.value)
    e2_row = Episode.objects.create(user=user, item=e2, status=Status.COMPLETED.value)
    e2_row.mark_watched_again()
    e2_row.mark_watched_again()
    e2_row.mark_watched_again()
    # e1: 60 × 1 = 60; e2: 60 × 4 = 240; total 300.
    s = build_stats(user)
    assert s.totals.minutes_watched == 300


def test_zero_runtime_doesnt_break(user):
    """Items with NULL runtime should contribute 0, not crash."""
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="z1",
        title="No Runtime", runtime=None, max_progress=1,
    )
    Movie.objects.create(user=user, item=item, status=Status.COMPLETED.value, repeats=5)
    s = build_stats(user)
    assert s.totals.minutes_watched == 0


def test_year_filter(user):
    m1 = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1",
        title="A", runtime=100, max_progress=1,
    )
    m2 = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="2",
        title="B", runtime=100, max_progress=1,
    )
    Movie.objects.create(user=user, item=m1, status=Status.COMPLETED.value,
                         end_date=date(2023, 6, 1))
    Movie.objects.create(user=user, item=m2, status=Status.COMPLETED.value,
                         end_date=date(2024, 6, 1))
    # A 2025 rewatch of m1 — events are the source of truth for yearly stats.
    from django.utils import timezone

    CompletionEvent.objects.create(
        user=user, item=m1, source="manual", precision="instant",
        completed_at=timezone.make_aware(datetime(2025, 3, 1, 12)),
    )

    assert build_stats(user, year=2023).totals.movies_completed == 1
    assert build_stats(user, year=2024).totals.movies_completed == 1
    assert build_stats(user, year=2025).totals.movies_completed == 1
    assert build_stats(user, year=None).totals.movies_completed == 3


def test_ranged_rewatch_leader_counts_original_outside_range(user):
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="range-rw",
        title="Range Rewatch", runtime=100,
    )
    first = timezone.now() - timedelta(days=100)
    recent = timezone.now() - timedelta(days=2)
    CompletionEvent.objects.create(user=user, item=item, completed_at=first)
    CompletionEvent.objects.create(user=user, item=item, completed_at=recent)

    stats = build_stats(user, range_key="7d")

    assert stats.totals.movies_completed == 1
    assert stats.rewatch_leaders[0].item == item
    assert stats.rewatch_leaders[0].repeats == 1


def test_missing_runtime_and_pages_are_reported(user):
    movie = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="unknown-runtime",
        title="Unknown Runtime", runtime=None,
    )
    book = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value, media_id="unknown-pages",
        title="Unknown Pages", max_progress=None,
    )
    CompletionEvent.objects.create(user=user, item=movie, completed_at=timezone.now())
    CompletionEvent.objects.create(user=user, item=book, completed_at=timezone.now())

    stats = build_stats(user)

    assert stats.totals.screen_events_missing_runtime == 1
    assert stats.totals.book_events_missing_pages == 1
    assert next(tile for tile in stats.tiles if tile["key"] == "Watch time")["value"] == "Unknown"
    assert "missing runtime" in next(
        tile for tile in stats.tiles if tile["key"] == "Watch time"
    )["sub"]
    assert "Page count unavailable" == next(
        tile for tile in stats.tiles if tile["key"] == "Books"
    )["sub"]


def test_episode_rating_is_consistent_between_top_rated_and_histogram(user):
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="rated-show", title="Show",
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="rated-show",
        season_number=1, parent=show, title="Season",
    )
    episode = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="rated-show",
        season_number=1, episode_number=1, parent=season, title="Rated Episode",
    )
    Episode.objects.create(user=user, item=episode, status=Status.COMPLETED.value, score=10)

    stats = build_stats(user)

    assert stats.score_histogram[10] == 1
    assert [row.item for row in stats.top_rated] == [episode]


def test_backdated_rewatch_has_date_precision(user):
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="dated-rw",
        title="Dated Rewatch", runtime=100,
    )
    row = Movie.objects.create(
        user=user, item=item, status=Status.COMPLETED.value, end_date=date(2020, 1, 1),
    )

    row.mark_watched_again(watched_on=date(2021, 2, 3))

    event = CompletionEvent.objects.filter(user=user, item=item).latest("completed_at")
    assert event.precision == "date"
    assert timezone.localtime(event.completed_at).date() == date(2021, 2, 3)


def test_non_year_range_ignores_year_for_annual_charts(user):
    stats = build_stats(user, range_key="7d", year=2001)

    assert stats.chart_year == timezone.localdate().year


def test_genres_are_deduplicated_and_include_search_subjects(user):
    book = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value, media_id="genres",
        title="Genres", max_progress=100,
        raw={
            "work": {"subjects": ["Drama", "Drama"]},
            "search": {"subject": ["Drama", "Fiction"]},
        },
    )
    Book.objects.create(user=user, item=book, status=Status.COMPLETED.value)

    assert dict(build_stats(user).top_genres) == {"Drama": 1, "Fiction": 1}
