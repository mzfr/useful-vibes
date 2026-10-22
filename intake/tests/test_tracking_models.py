"""Tracking model behavior: constraints, process hooks, rewatch."""
from __future__ import annotations

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import Book, Episode, Movie

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def movie_item():
    return Item.objects.create(
        source="tmdb",
        media_type=MediaType.MOVIE.value,
        media_id="27205",
        title="Inception",
        runtime=148,
        max_progress=1,
    )


@pytest.fixture
def book_item():
    return Item.objects.create(
        source="openlibrary",
        media_type=MediaType.BOOK.value,
        media_id="OL27448W",
        title="LOTR",
        max_progress=1178,
    )


def test_score_range_constraint(user, movie_item):
    Movie.objects.create(user=user, item=movie_item, score=10)
    with pytest.raises(IntegrityError):
        Movie.objects.create(user=user, item=movie_item, score=11)


def test_unique_per_user_item(user, movie_item):
    Movie.objects.create(user=user, item=movie_item)
    with pytest.raises(IntegrityError):
        Movie.objects.create(user=user, item=movie_item)


def test_save_rejects_wrong_media_type(user, book_item):
    # Booking a Movie row against a book Item must fail.
    with pytest.raises(ValueError):
        Movie(user=user, item=book_item).save()


def test_process_progress_clamps_to_max(user, book_item):
    b = Book.objects.create(user=user, item=book_item, progress=99999)
    assert b.progress == 1178  # clamped to max_progress


def test_progress_implies_in_progress(user, book_item):
    b = Book.objects.create(
        user=user, item=book_item, progress=10, status=Status.PLANNING.value
    )
    assert b.status == Status.IN_PROGRESS.value
    assert b.start_date == date.today()


def test_completion_auto_pegs_progress_and_dates(user, movie_item):
    m = Movie.objects.create(user=user, item=movie_item, status=Status.COMPLETED.value)
    assert m.progress == 1
    assert m.end_date == date.today()
    assert m.start_date == date.today()


def test_progress_at_max_auto_completes(user, book_item):
    b = Book.objects.create(
        user=user, item=book_item, status=Status.IN_PROGRESS.value, progress=1178
    )
    assert b.status == Status.COMPLETED.value
    assert b.end_date == date.today()


def test_mark_watched_again_bumps_repeats_and_writes_history(user, movie_item):
    m = Movie.objects.create(user=user, item=movie_item, status=Status.COMPLETED.value)
    assert m.repeats == 0
    assert m.history.count() == 1

    m.mark_watched_again()
    m.refresh_from_db()
    assert m.repeats == 1
    assert m.history.count() == 2

    m.mark_watched_again()
    m.refresh_from_db()
    assert m.repeats == 2


def test_episode_constraints_require_both_numbers():
    # Ep Items with only one number should be rejected by the Item-level CHECK.
    with pytest.raises(IntegrityError):
        Item.objects.create(
            source="tmdb",
            media_type=MediaType.EPISODE.value,
            media_id="1399",
            season_number=1,
            episode_number=None,
            title="bad",
        )


def test_episode_tracking_needs_episode_item(user):
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="1399", title="GoT"
    )
    with pytest.raises(ValueError):
        Episode(user=user, item=show).save()
