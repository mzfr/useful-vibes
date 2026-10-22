from __future__ import annotations

import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from httpx import Response

from api.models import APIToken
from core.models import AppSettings
from core.settings_store import tmdb_api_key
from imports.models import ImportJob
from tracking.models import Book

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


def test_imports_page_renders_imports_token_status_and_exports(client, user):
    APIToken.mint(user, ttl="never", label="Life Logger")
    ImportJob.objects.create(user=user, kind="trakt", state="done", processed=10, total=10)

    response = client.get(reverse("imports:index"))

    assert response.status_code == 200
    assert reverse("imports:index") == "/settings/"
    assert client.get("/settings/sharing/").status_code == 404
    body = response.content.decode()
    assert "Settings" in body
    assert "Trakt export" in body
    assert "Books CSV" in body
    assert "Recent jobs" in body
    assert "Shows + movies CSV" in body
    assert "intake_••••" in body


@respx.mock
def test_settings_saves_encrypted_tmdb_key(client):
    respx.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=Response(200, json={})
    )

    response = client.post(reverse("imports:save_tmdb_key"), {"tmdb_api_key": "secret-key"})

    assert response.status_code == 302
    stored = AppSettings.objects.get()
    assert stored.tmdb_api_key_encrypted != "secret-key"
    assert "secret-key" not in stored.tmdb_api_key_encrypted
    assert tmdb_api_key() == "secret-key"


@respx.mock
def test_settings_rejects_invalid_tmdb_key(client):
    respx.get("https://api.themoviedb.org/3/configuration").mock(
        return_value=Response(401, json={"status_message": "Invalid API key"})
    )

    response = client.post(reverse("imports:save_tmdb_key"), {"tmdb_api_key": "bad"})

    assert response.status_code == 302
    assert not AppSettings.objects.exists()


def _book_review_job(user):
    return ImportJob.objects.create(
        user=user,
        kind="csv_books",
        state="done",
        processed=1,
        total=1,
        needs_review=[{
            "reason": "no Open Library match",
            "row": {
                "Title": "A Difficult Book",
                "Authors": "An Author",
                "ISBN/UID": "9780000000001",
                "Read Status": "read",
                "Last Date Read": "2024/03/12",
                "Star Rating": "4.5",
            },
        }],
    )


def test_book_review_job_renders_original_row(client, user):
    job = _book_review_job(user)

    response = client.get(reverse("imports:job", args=[job.pk]))

    assert response.status_code == 200
    body = response.content.decode()
    assert "A Difficult Book" in body
    assert "An Author" in body
    assert "9780000000001" in body
    assert "Search Open Library" in body


@respx.mock
def test_book_review_search_and_match(client, user):
    job = _book_review_job(user)
    respx.get("https://openlibrary.org/search.json").mock(return_value=Response(200, json={
        "numFound": 1,
        "docs": [{
            "key": "/works/OL42W",
            "title": "A Difficult Book",
            "author_name": ["An Author"],
            "first_publish_year": 2020,
            "cover_i": 42,
            "number_of_pages_median": 240,
            "isbn": ["9780000000001"],
        }],
    }))

    search = client.get(
        reverse("imports:review_book_search", args=[job.pk, 0]),
        {"q": "A Difficult Book An Author"},
    )
    assert search.status_code == 200
    assert "OL42W" in search.content.decode()
    selection_token = client.session[f"book_review:{job.pk}:0:OL42W"]

    response = client.post(
        reverse("imports:review_book_match", args=[job.pk, 0]),
        {
            "olid": "OL42W",
            "selection_token": selection_token,
            "title": "A Difficult Book — An Author",
            "year": "2020",
            "pages": "240",
            "isbn": "9780000000001",
        },
    )

    assert response.status_code == 302
    book = Book.objects.get(user=user)
    assert book.item.media_id == "OL42W"
    assert book.status == "completed"
    assert book.score == 9
    assert book.end_date.isoformat() == "2024-03-12"
    job.refresh_from_db()
    assert job.needs_review[0]["resolution"] == "matched"
    assert job.pending_review_count == 0


def test_book_review_rejects_unsearched_match(client, user):
    job = _book_review_job(user)

    response = client.post(
        reverse("imports:review_book_match", args=[job.pk, 0]),
        {"olid": "OL42W", "title": "Forged", "selection_token": "bad"},
    )

    assert response.status_code == 400
    assert not Book.objects.filter(user=user).exists()


def test_book_review_skip_preserves_original_row(client, user):
    job = _book_review_job(user)

    response = client.post(reverse("imports:review_book_skip", args=[job.pk, 0]))

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.needs_review[0]["resolution"] == "skipped"
    assert job.needs_review[0]["row"]["Title"] == "A Difficult Book"
    assert job.pending_review_count == 0


def test_book_review_is_user_scoped(client, user):
    other = get_user_model().objects.create_user(username="review-other", password="p")
    job = _book_review_job(other)

    assert client.get(reverse("imports:review_book_search", args=[job.pk, 0])).status_code == 404
    assert client.post(reverse("imports:review_book_skip", args=[job.pk, 0])).status_code == 404


def test_import_jobs_partial_is_user_scoped(client, user):
    other = get_user_model().objects.create_user(username="other", password="p")
    own = ImportJob.objects.create(user=user, kind="csv_books", state="done")
    other_job = ImportJob.objects.create(user=other, kind="trakt", state="failed")

    response = client.get(reverse("imports:jobs"))

    assert response.status_code == 200
    body = response.content.decode()
    assert f"#{own.pk}" in body
    assert f"#{other_job.pk}" not in body
