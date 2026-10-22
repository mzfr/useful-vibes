"""Home page renders without errors — catches URL-reverse and template bugs
that service-level tests miss."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import Book, CompletionEvent, Episode

pytestmark = pytest.mark.django_db


def _setup(user):
    """A show with a watched ep + a new aired season, and one with unwatched eps."""
    # Show A: watched S1E1, S2 exists aired but no local episodes → new season.
    show_a = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="990101",
        title="Show A", raw={"seasons": [
            {"season_number": 1, "air_date": "2023-01-01", "episode_count": 3},
            {"season_number": 2, "air_date": "2026-01-01", "episode_count": 3},
        ]},
    )
    s1 = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="990101",
        season_number=1, parent=show_a, title="S1", metadata_complete=True,
    )
    e1 = Item.objects.create(
        source="tmdb", media_type=MediaType.EPISODE.value, media_id="990101",
        season_number=1, episode_number=1, parent=s1, title="E1",
        release_date=date.today() - timedelta(days=30), metadata_complete=True,
    )
    Episode.objects.create(user=user, item=e1, status=Status.COMPLETED.value)

    # Show B: S1E1,E2 unwatched but aired → new episodes group.
    show_b = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="990102",
        title="Show B",
    )
    s1b = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="990102",
        season_number=1, parent=show_b, title="S1", metadata_complete=True,
    )
    for n in (1, 2):
        Item.objects.create(
            source="tmdb", media_type=MediaType.EPISODE.value, media_id="990102",
            season_number=1, episode_number=n, parent=s1b, title=f"E{n}",
            release_date=date.today() - timedelta(days=1), metadata_complete=True,
        )
    # Engage show B by watching its first episode.
    e1b = Item.objects.get(media_type="episode", media_id="990102", episode_number=1)
    Episode.objects.create(user=user, item=e1b, status=Status.COMPLETED.value)


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="home_test")


def test_home_renders_with_seasons_and_episodes(client, user):
    _setup(user)
    client.force_login(user)
    resp = client.get("/")
    assert resp.status_code == 200, resp.content[:1000]


def test_home_history_shows_media_badges(client, user):
    book = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value,
        media_id="OL2W", title="Finished Book",
    )
    CompletionEvent.objects.create(user=user, item=book, completed_at=timezone.now())
    client.force_login(user)

    response = client.get("/")

    assert response.status_code == 200
    assert b">BOOK<" in response.content


def test_home_does_not_show_in_progress_books_in_up_next(client, user):
    book = Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value,
        media_id="OL1W", title="Currently Reading",
    )
    Book.objects.create(user=user, item=book, status=Status.IN_PROGRESS.value)
    client.force_login(user)

    response = client.get("/")

    assert response.status_code == 200
    assert b"Currently Reading" not in response.content


def test_history_defaults_to_month_calendar(client, user):
    client.force_login(user)

    response = client.get("/history/")

    assert response.status_code == 200
    assert response.context["view"] == "month"
    assert len(response.context["days"]) in {35, 42}


def test_history_week_view_still_has_seven_days(client, user):
    client.force_login(user)

    response = client.get("/history/", {"view": "week"})

    assert response.status_code == 200
    assert response.context["view"] == "week"
    assert len(response.context["days"]) == 7


def test_full_history_shows_episode_badge(client, user):
    _setup(user)
    episode = Item.objects.get(media_type=MediaType.EPISODE.value, media_id="990101")
    CompletionEvent.objects.create(user=user, item=episode, completed_at=timezone.now())
    client.force_login(user)

    response = client.get("/history/")

    assert response.status_code == 200
    assert b">EP<" in response.content


def test_home_renders_with_query(client, user):
    _setup(user)
    client.force_login(user)
    resp = client.get("/", {"q": "Show", "type": "all"})
    assert resp.status_code == 200, resp.content[:1000]


def test_home_search_ranks_exact_before_partial_matches(client, user):
    Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1001",
        title="Highway",
    )
    Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1002",
        title="Lost Highway",
    )
    client.force_login(user)

    response = client.get("/", {"q": "Highway", "type": "all"})

    assert response.status_code == 200
    assert response.content.index(b">Highway<") < response.content.index(b">Lost Highway<")


def test_home_search_tolerates_typo_and_accents(client, user):
    Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1003",
        title="Highway",
    )
    Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1004",
        title="Amélie",
    )
    client.force_login(user)

    typo = client.get("/", {"q": "highwy", "type": "all"})
    accent = client.get("/", {"q": "amelie", "type": "all"})

    assert b">Highway<" in typo.content
    assert "Amélie" in accent.content.decode()


def test_home_search_honors_media_filter(client, user):
    Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="1005",
        title="Shared Name",
    )
    Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="1006",
        title="Shared Name",
    )
    client.force_login(user)

    response = client.get("/", {"q": "Shared Name", "type": "movie"})

    assert response.content.count(b">Shared Name<") == 1
    # The type label sits in the card's caption line under the title.
    body = response.content.decode()
    caption = body.rsplit("Shared Name", 1)[1]
    assert "movie" in caption.split("</p>", 2)[1]
