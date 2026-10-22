"""
Open Library provider — books only.

OL uses two main identifier types:
  - work key:   /works/OL12345W   (the abstract work — what we store)
  - edition:    /books/OL67890M   (a specific printing — used for page counts)

We index by the work OLID (no leading slash) and resolve a representative edition
on details() to surface page count and cover image.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from core.enums import MediaType

from .base import (
    EpisodeMetadata,
    Metadata,
    NotSupported,
    Provider,
    ProviderError,
    SearchHit,
    SearchResults,
    SeasonMetadata,
    _ensure,
)
from .http import HTTPClient


def _cover_url(cover_id: int | None, size: str = "L") -> str:
    if not cover_id:
        return ""
    return f"https://covers.openlibrary.org/b/id/{cover_id}-{size}.jpg"


def _strip_olid(s: str) -> str:
    # Accept both "OL123W" and "/works/OL123W" — return bare OLID.
    return s.rsplit("/", 1)[-1]


def _year_to_date(value: int | str | None) -> date | None:
    """Extract a year from OL's messy date strings ("May 1, 1999", "1954", 2005)."""
    if not value:
        return None
    if isinstance(value, int):
        year = value
    else:
        m = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", str(value))
        if not m:
            return None
        year = int(m.group(1))
    try:
        return date(year, 1, 1)
    except ValueError:
        return None


def _first_parseable_date(*candidates) -> date | None:
    """Try each candidate independently — edition publish_date is prose and
    often unparseable while the work's first_publish_date is a clean year."""
    for c in candidates:
        d = _year_to_date(c)
        if d is not None:
            return d
    return None


class OpenLibraryProvider(Provider):
    source = "openlibrary"
    supported_types = {MediaType.BOOK.value}

    def __init__(self) -> None:
        self._http = HTTPClient(base_url="https://openlibrary.org")

    def search(self, media_type: str, query: str, page: int = 1) -> SearchResults:
        _ensure(self, media_type)
        # OL's search endpoint accepts q + page; fields= keeps the payload small.
        data = self._http.get_json(
            "/search.json",
            q=query,
            page=page,
            limit=20,
            fields=(
                "key,title,author_name,first_publish_year,cover_i,edition_key,"
                "isbn,number_of_pages_median"
            ),
        )
        hits = []
        for r in data.get("docs", []):
            work_key = r.get("key") or ""
            olid = _strip_olid(work_key)
            if not olid:
                continue
            authors = r.get("author_name") or []
            title = r.get("title") or ""
            if authors:
                title = f"{title} — {authors[0]}"
            hits.append(
                SearchHit(
                    source=self.source,
                    media_type=MediaType.BOOK.value,
                    media_id=olid,
                    title=title,
                    image=_cover_url(r.get("cover_i"), "M"),
                    year=r.get("first_publish_year"),
                    runtime=r.get("number_of_pages_median"),
                    raw=r,
                )
            )
        per_page = 20
        total = data.get("numFound", len(hits))
        total_pages = max(1, (total + per_page - 1) // per_page)
        return SearchResults(
            hits=hits,
            page=page,
            total_pages=total_pages,
            total_results=total,
        )

    def details(self, media_type: str, media_id: str) -> Metadata:
        _ensure(self, media_type)
        olid = _strip_olid(media_id)
        work = self._http.get_json(f"/works/{olid}.json")

        # Pull a representative edition for page count + ISBN.
        edition_data = self._http.get_json(
            f"/works/{olid}/editions.json", limit=1
        )
        edition = (edition_data.get("entries") or [None])[0] or {}

        # Synopsis can be a string or {"value": "..."} on OL.
        desc = work.get("description")
        if isinstance(desc, dict):
            desc = desc.get("value", "")

        # Authors arrive as [{"author": {"key": "/authors/OL..."}}, ...] — we resolve
        # names lazily to avoid an N+1 storm; just grab the first as a label.
        author_label = self._first_author_label(work.get("authors", []))

        cover_id = None
        if work.get("covers"):
            cover_id = work["covers"][0]
        elif edition.get("covers"):
            cover_id = edition["covers"][0]

        isbns: list[str] = []
        for key in ("isbn_13", "isbn_10"):
            isbns.extend(edition.get(key, []))

        title = work.get("title", "")
        if author_label:
            title = f"{title} — {author_label}"

        external_ids: dict[str, str] = {"olid": olid}
        if isbns:
            external_ids["isbn"] = isbns[0]

        pages = edition.get("number_of_pages")

        return Metadata(
            source=self.source,
            media_type=MediaType.BOOK.value,
            media_id=olid,
            title=title,
            image=_cover_url(cover_id),
            synopsis=desc or "",
            genres=[s for s in (work.get("subjects") or [])[:8] if isinstance(s, str)],
            release_date=_first_parseable_date(
                work.get("first_publish_date"), edition.get("publish_date")
            ),
            runtime=pages,
            max_progress=pages,
            external_ids=external_ids,
            raw={"work": work, "edition": edition},
        )

    def _first_author_label(self, authors: list[dict[str, Any]]) -> str:
        if not authors:
            return ""
        first = authors[0]
        key = ""
        # Two shapes seen in the wild.
        if isinstance(first, dict):
            if "author" in first and isinstance(first["author"], dict):
                key = first["author"].get("key", "")
            else:
                key = first.get("key", "")
        # Provider payload is untrusted. Accept only canonical OL author paths;
        # absolute URLs or traversal must never reach the HTTP client.
        if not re.fullmatch(r"/authors/OL\d+A", key):
            return ""
        try:
            data = self._http.get_json(f"{key}.json")
        except ProviderError:
            return ""
        return data.get("name", "") or ""

    # Books are flat — TV-style endpoints are not supported.
    def season(self, media_id: str, season_number: int) -> SeasonMetadata:
        raise NotSupported("Open Library has no seasons.")

    def episode(
        self, media_id: str, season_number: int, episode_number: int
    ) -> EpisodeMetadata:
        raise NotSupported("Open Library has no episodes.")
