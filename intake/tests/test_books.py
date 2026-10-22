from __future__ import annotations

from datetime import date

import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from httpx import Response

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import Book

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="reader", password="p")


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


def _book(user, *, olid="OL1W", title="A Book — An Author", status=Status.PLANNING.value):
    item = Item.objects.create(
        source="openlibrary",
        media_type=MediaType.BOOK.value,
        media_id=olid,
        title=title,
        max_progress=300,
        raw={"search": {"title": title.split(" — ")[0], "author_name": ["An Author"]}},
    )
    tracking = Book.objects.create(user=user, item=item, status=status)
    return item, tracking


def test_books_defaults_to_reading_then_to_read(client, user):
    _book(user, olid="OL1W", status=Status.PLANNING.value)
    reading, _ = _book(user, olid="OL2W", title="Reading Now — An Author", status=Status.IN_PROGRESS.value)

    response = client.get(reverse("tracking:books"))

    assert response.status_code == 200
    assert b"Reading Now" in response.content
    assert b"A Book" not in response.content
    assert reading.get_absolute_url() == reverse("tracking:book_detail", args=["OL2W"])
    legacy = reverse("tracking:legacy_details", args=["openlibrary", "book", "OL2W"])
    assert client.get(legacy).status_code == 301


@respx.mock
def test_books_inline_search_and_add(client, user, load_fixture):
    respx.get("https://openlibrary.org/search.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_search.json"))
    )
    respx.get("https://openlibrary.org/works/OL27448W.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_work.json"))
    )
    respx.get("https://openlibrary.org/works/OL27448W/editions.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_editions.json"))
    )
    respx.get("https://openlibrary.org/authors/OL26320A.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_author.json"))
    )

    response = client.get(reverse("tracking:book_search"), {"q": "Tolkien"})
    assert response.status_code == 200
    assert b"OL27448W" in response.content

    response = client.post(reverse("tracking:book_add"), {"olid": "OL27448W"})
    assert response.status_code == 200
    book = Book.objects.get(user=user)
    assert book.status == Status.PLANNING.value
    assert b"Added to To Read" in response.content


def test_books_filter_counts_and_rows(client, user):
    _book(user, olid="OL1W", status=Status.PLANNING.value)
    _book(user, olid="OL2W", title="Done Book — An Author", status=Status.COMPLETED.value)

    response = client.get(reverse("tracking:books"), {"status": "all"})

    assert response.status_code == 200
    assert b"A Book" in response.content
    assert b"Done Book" in response.content
    assert b"2 tracked" in response.content


def test_books_filter_and_sort_share_the_selected_status(client, user):
    _book(user, olid="OL1W", title="Reading Book — An Author", status=Status.IN_PROGRESS.value)
    _book(user, olid="OL2W", title="Read Book — An Author", status=Status.COMPLETED.value)

    response = client.get(reverse("tracking:books"))

    assert response.status_code == 200
    body = response.content.decode()
    assert 'x-data="{ selected: \'reading\' }"' in body
    assert 'name="status" :value="selected" value="reading"' in body
    assert "?status=read&sort=added_desc" in body

    sorted_read = client.get(
        reverse("tracking:books"), {"status": "read", "sort": "finished_desc"},
        HTTP_HX_REQUEST="true",
    )
    assert b"Read Book" in sorted_read.content
    assert b"Reading Book" not in sorted_read.content


def test_books_sort_by_title_and_finished_date(client, user):
    _book(user, olid="OL1W", title="Zulu — An Author", status=Status.COMPLETED.value)
    _book(user, olid="OL2W", title="Alpha — An Author", status=Status.COMPLETED.value)
    first = client.get(reverse("tracking:books"), {"status": "all", "sort": "title_asc"})
    assert first.content.index(b"Alpha") < first.content.index(b"Zulu")

    Book.objects.filter(item__media_id="OL1W").update(end_date="2024-01-01")
    Book.objects.filter(item__media_id="OL2W").update(end_date="2025-01-01")
    finished = client.get(
        reverse("tracking:books"), {"status": "read", "sort": "finished_desc"},
    )
    assert finished.content.index(b"Alpha") < finished.content.index(b"Zulu")


def test_book_detail_and_actions(client, user):
    item, tracking = _book(user, status=Status.PLANNING.value)

    response = client.get(reverse("tracking:book_detail", args=[item.media_id]))
    assert response.status_code == 200
    assert b"A Book" in response.content

    response = client.post(
        reverse("tracking:book_status", args=[item.pk]),
        {"status": Status.IN_PROGRESS.value},
    )
    assert response.status_code == 302
    tracking.refresh_from_db()
    assert tracking.status == Status.IN_PROGRESS.value
    assert tracking.start_date == timezone.localdate()

    response = client.post(
        reverse("tracking:book_progress", args=[item.pk]),
        {"progress": "120", "progress_unit": "pages"},
    )
    assert response.status_code == 302
    tracking.refresh_from_db()
    assert tracking.progress == 120

    response = client.post(
        reverse("tracking:book_progress", args=[item.pk]),
        {"progress": "60", "progress_unit": "percent"},
    )
    assert response.status_code == 302
    tracking.refresh_from_db()
    assert tracking.progress == 180

    response = client.post(
        reverse("tracking:book_status", args=[item.pk]),
        {"status": Status.COMPLETED.value},
    )
    assert response.status_code == 302
    tracking.refresh_from_db()
    assert tracking.status == Status.COMPLETED.value
    assert tracking.end_date == timezone.localdate()

    response = client.post(reverse("tracking:book_rate", args=[item.pk]), {"score": "8"})
    assert response.status_code == 302
    tracking.refresh_from_db()
    assert tracking.score == 8

    client.post(
        reverse("tracking:book_status", args=[item.pk]),
        {"status": Status.IN_PROGRESS.value},
    )
    response = client.post(
        reverse("tracking:book_dates", args=[item.pk]),
        {"start_date": "2024-03-11", "end_date": ""},
    )
    assert response.status_code == 302
    tracking.refresh_from_db()
    assert tracking.status == Status.IN_PROGRESS.value
    assert tracking.start_date == date(2024, 3, 11)
    assert tracking.end_date is None



def test_book_percentage_requires_known_page_count(client, user):
    item, _tracking = _book(user)
    item.max_progress = None
    item.save(update_fields=["max_progress"])

    response = client.post(
        reverse("tracking:book_progress", args=[item.pk]),
        {"progress": "60", "progress_unit": "percent"},
    )

    assert response.status_code == 400


def test_book_actions_reject_bad_values(client, user):
    item, _ = _book(user)
    assert client.post(reverse("tracking:book_status", args=[item.pk]), {"status": "bad"}).status_code == 400
    assert client.post(reverse("tracking:book_progress", args=[item.pk]), {"progress": "-1"}).status_code == 400
    assert client.post(
        reverse("tracking:book_progress", args=[item.pk]),
        {"progress": "101", "progress_unit": "percent"},
    ).status_code == 400
    assert client.post(
        reverse("tracking:book_progress", args=[item.pk]),
        {"progress": "20", "progress_unit": "chapters"},
    ).status_code == 400
    assert client.post(reverse("tracking:book_rate", args=[item.pk]), {"score": "11"}).status_code == 400
    assert client.post(
        reverse("tracking:book_dates", args=[item.pk]),
        {"start_date": "2025-01-02", "end_date": "2025-01-01"},
    ).status_code == 400


def test_book_actions_do_not_redirect_off_site(client, user):
    item, _ = _book(user)
    response = client.post(
        reverse("tracking:book_status", args=[item.pk]),
        {"status": Status.IN_PROGRESS.value, "next": "https://example.com"},
    )
    assert response.status_code == 302
    assert response.url == reverse("tracking:books") + "?status=all&sort=added_desc"
