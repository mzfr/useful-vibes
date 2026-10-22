"""Home search: local catalog first, TMDB only as a fallback."""
import pytest
from django.urls import reverse

from core.enums import MediaType
from core.models import Item
from tracking.models import Movie


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user(username="searcher", password="pw")


def test_local_hit_does_not_trigger_remote_search(client, user, db):
    Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="55",
        title="Local Only Movie",
    )
    client.force_login(user)

    body = client.get("/", {"q": "Local Only Movie"}).content.decode()

    assert "Local Only Movie" in body
    assert "in your library" in body
    # No lazy-loading stub means TMDB is never contacted.
    assert reverse("core:remote_search") not in body


def test_missing_title_queues_remote_search(client, user, db):
    client.force_login(user)

    body = client.get("/", {"q": "Totally Unknown Film"}).content.decode()

    assert reverse("core:remote_search") in body
    assert "Checking TMDB" in body


def test_remote_search_returns_tmdb_hits(client, user, fake_tmdb, db):
    client.force_login(user)

    body = client.get(
        reverse("core:remote_search"), {"q": "Dune Part Three", "type": "movie"}
    ).content.decode()

    assert "Dune Part Three (movie)" in body
    assert "not in your library yet" in body


def test_books_are_excluded_from_home_search(client, user, db):
    Item.objects.create(
        source="openlibrary", media_type=MediaType.BOOK.value, media_id="OL1W",
        title="Some Book Title",
    )
    client.force_login(user)

    body = client.get("/", {"q": "Some Book Title"}).content.decode()

    # The book exists locally but this box is movies + shows only, so it
    # falls through to the remote path instead of matching.
    assert "Some Book Title</p>" not in body
    assert "Checking TMDB" in body


def test_add_remote_resolves_and_watchlists(client, user, fake_tmdb, db):
    client.force_login(user)

    response = client.post(
        reverse("tracking:add_remote", args=["tmdb", "movie", "9001"])
    )

    assert response.status_code == 200
    item = Item.objects.get(source="tmdb", media_type="movie", media_id="9001")
    assert Movie.objects.get(user=user, item=item).status == "planning"
