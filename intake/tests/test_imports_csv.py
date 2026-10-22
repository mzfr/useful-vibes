"""CSV importers: format detection, mapping, and the needs_review path."""
from __future__ import annotations

import io

import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import override_settings
from httpx import Response

from imports import csv_books, csv_movies_shows
from providers import registry
from tracking.models import Book, Episode, Movie

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def reset_provider_cache():
    registry.reset_cache()
    yield
    registry.reset_cache()


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


# ---------------------------------------------------------------------- movies


TRAKT_CSV = (
    "rated_at,title,year,trakt_rating,tmdb_id,type,season,episode\n"
    "2024-01-15,Inception,2010,9,27205,movie,,\n"
    "2024-02-20,Bad Row,,,,unknown,,\n"
)


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_csv_movies_shows_imports_movie(user, load_fixture):
    respx.get("https://api.themoviedb.org/3/movie/27205").mock(
        return_value=Response(200, json=load_fixture("tmdb_movie_details.json"))
    )

    job = csv_movies_shows.import_csv(user, io.StringIO(TRAKT_CSV))

    assert job.state == "done"
    assert job.total == 2
    assert job.processed == 2
    assert Movie.objects.filter(user=user).count() == 1
    movie = Movie.objects.get(user=user)
    assert movie.score == 9
    assert movie.status == "completed"
    # One unknown row must be flagged.
    assert len(job.needs_review) == 1
    assert job.needs_review[0]["reason"] == "unknown row"


@override_settings(TMDB_API_KEY="fake")
@respx.mock
def test_csv_movies_shows_imports_episode(user, load_fixture):
    csv = (
        "rated_at,title,year,trakt_rating,tmdb_id,type,season,episode\n"
        "2024-01-15,Game of Thrones,2011,10,1399,episode,1,1\n"
    )
    respx.get("https://api.themoviedb.org/3/tv/1399").mock(
        return_value=Response(200, json=load_fixture("tmdb_tv_details.json"))
    )
    respx.get("https://api.themoviedb.org/3/tv/1399/season/1").mock(
        return_value=Response(200, json=load_fixture("tmdb_season.json"))
    )

    job = csv_movies_shows.import_csv(user, io.StringIO(csv))

    assert job.state == "done"
    eps = Episode.objects.filter(user=user)
    assert eps.count() == 1
    e = eps.first()
    assert e.score == 10
    assert e.item.episode_number == 1


# ----------------------------------------------------------------------- books

GOODREADS_CSV = (
    'Book Id,Title,Author,ISBN,ISBN13,My Rating,Exclusive Shelf,Date Read,Date Added\n'
    '1,"The Lord of the Rings","J.R.R. Tolkien","",="9780618640157",5,read,2023/06/01,2023/01/01\n'
    '2,"Unknown Book","Nobody","","",0,to-read,,2023/02/01\n'
)


@respx.mock
def test_csv_books_goodreads_resolves_via_isbn(user, load_fixture):
    # Search results returned via ISBN lookup
    respx.get("https://openlibrary.org/search.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_search.json"))
    )
    job = csv_books.import_csv(user, io.StringIO(GOODREADS_CSV))

    assert job.state == "done"
    assert job.total == 2

    # Both rows resolve to the same OL fixture, so they dedup onto one Book row
    # (update_or_create on natural key). The completed one wins because it's
    # processed last with the most info.
    books = Book.objects.filter(user=user)
    assert books.count() == 1
    b = books.first()
    assert b.score == 10  # 5 stars → 10/10
    assert b.status == "completed"
    # Matching uses only search responses; no per-book work/edition/author fan-out.
    assert all(call.request.url.path == "/search.json" for call in respx.calls)


def test_book_csv_detects_storygraph():
    storygraph_headers = [
        "Title", "Authors", "ISBN/UID", "Format", "Read Status",
        "Date Added", "Last Date Read", "Star Rating",
    ]
    assert csv_books._detect_format(storygraph_headers) == "storygraph"


def test_book_csv_detects_goodreads():
    goodreads_headers = [
        "Book Id", "Title", "Author", "ISBN", "ISBN13",
        "My Rating", "Exclusive Shelf", "Date Read",
    ]
    assert csv_books._detect_format(goodreads_headers) == "goodreads"


def test_book_csv_unknown_format(user):
    bad = "Col1,Col2\nfoo,bar\n"
    job = csv_books.import_csv(user, io.StringIO(bad))
    assert job.state == "failed"
    assert "format" in job.error.lower()


# --------------------------------------------------------------------- exports


def test_export_movies_shows_csv(user):
    from core.enums import MediaType, Status
    from core.models import Item
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="27205",
        title="Inception",
    )
    Movie.objects.create(user=user, item=item, status=Status.COMPLETED.value, score=9)
    buf = io.StringIO()
    csv_movies_shows.export_csv(user, buf)
    output = buf.getvalue()
    assert "Inception" in output
    assert "27205" in output
    assert "movie" in output
