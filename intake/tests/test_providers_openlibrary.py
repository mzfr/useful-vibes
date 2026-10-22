"""Open Library provider tests — fully offline."""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from providers.base import NotSupported
from providers.openlibrary import OpenLibraryProvider


@pytest.fixture
def ol():
    return OpenLibraryProvider()


@respx.mock
def test_search(ol, load_fixture):
    respx.get("https://openlibrary.org/search.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_search.json"))
    )

    results = ol.search("book", "lord of the rings")

    assert results.total_results == 1
    hit = results.hits[0]
    assert hit.source == "openlibrary"
    assert hit.media_type == "book"
    assert hit.media_id == "OL27448W"
    # Author is appended to the title for disambiguation.
    assert "Tolkien" in hit.title
    assert hit.year == 1954
    assert "14625765" in hit.image


@respx.mock
def test_details(ol, load_fixture):
    respx.get("https://openlibrary.org/works/OL27448W.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_work.json"))
    )
    respx.get("https://openlibrary.org/works/OL27448W/editions.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_editions.json"))
    )
    respx.get("https://openlibrary.org/authors/OL26320A.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_author.json"))
    )

    md = ol.details("book", "OL27448W")

    assert md.media_id == "OL27448W"
    assert "Tolkien" in md.title
    assert md.max_progress == 1178           # page count from the edition
    assert md.runtime == 1178                # surfaces as runtime too
    assert md.external_ids["olid"] == "OL27448W"
    assert md.external_ids["isbn"] == "9780618640157"
    assert "Fantasy" in md.genres
    assert md.synopsis.startswith("An epic")


@respx.mock
def test_details_accepts_prefixed_id(ol, load_fixture):
    respx.get("https://openlibrary.org/works/OL27448W.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_work.json"))
    )
    respx.get("https://openlibrary.org/works/OL27448W/editions.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_editions.json"))
    )
    respx.get("https://openlibrary.org/authors/OL26320A.json").mock(
        return_value=Response(200, json=load_fixture("openlibrary_author.json"))
    )

    md = ol.details("book", "/works/OL27448W")
    assert md.media_id == "OL27448W"


def test_seasons_and_episodes_not_supported(ol):
    with pytest.raises(NotSupported):
        ol.season("OL27448W", 1)
    with pytest.raises(NotSupported):
        ol.episode("OL27448W", 1, 1)
