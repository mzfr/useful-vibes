"""
CSV importer for books — autodetects Goodreads and Storygraph exports.

Goodreads columns (subset we use):
  Book Id, Title, Author, ISBN, ISBN13, My Rating, Exclusive Shelf,
  Date Read, Date Added

Storygraph columns:
  Title, Authors, ISBN/UID, Format, Read Status, Date Added,
  Last Date Read, Star Rating, ...

We resolve to Open Library via ISBN first, falling back to a title+author
search. Unmappable rows land in `needs_review`.
"""
from __future__ import annotations

import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import IO

from core.enums import MediaType, Status
from providers import registry
from providers.base import Metadata, ProviderError
from tracking import services
from tracking.models import Book

from .models import ImportJob


def _detect_format(fieldnames: list[str]) -> str:
    s = {f.strip().lower() for f in (fieldnames or [])}
    if "exclusive shelf" in s and "date read" in s:
        return "goodreads"
    if "read status" in s and "authors" in s:
        return "storygraph"
    return "unknown"


def _parse_date(raw: str | None):
    if not raw:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _rating(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        # Goodreads ratings 0-5; Storygraph star ratings 0-5 too. Scale to 1-10.
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return min(10, max(1, round(v * 2)))


def _status_from_shelf(raw: str | None) -> str:
    if not raw:
        return Status.PLANNING.value
    s = raw.strip().lower()
    if s in ("read", "currently-reading", "to-read"):
        return {
            "read": Status.COMPLETED.value,
            "currently-reading": Status.IN_PROGRESS.value,
            "to-read": Status.PLANNING.value,
        }[s]
    if s in ("read", "currently reading", "to read", "did-not-finish", "did not finish"):
        return {
            "read": Status.COMPLETED.value,
            "currently reading": Status.IN_PROGRESS.value,
            "to read": Status.PLANNING.value,
            "did-not-finish": Status.DROPPED.value,
            "did not finish": Status.DROPPED.value,
        }[s]
    return Status.PLANNING.value


def _normalize_row(row: dict, fmt: str) -> dict | None:
    """Return a unified dict: title, author, isbn, rating, status, date_read."""
    if fmt == "goodreads":
        return {
            "title": (row.get("Title") or "").strip(),
            "author": (row.get("Author") or "").strip(),
            "isbn": (row.get("ISBN13") or row.get("ISBN") or "").strip().replace('="', "").replace('"', ""),
            "rating": _rating(row.get("My Rating")),
            "status": _status_from_shelf(row.get("Exclusive Shelf")),
            "date_read": _parse_date(row.get("Date Read")),
        }
    if fmt == "storygraph":
        return {
            "title": (row.get("Title") or "").strip(),
            "author": (row.get("Authors") or "").strip(),
            "isbn": (row.get("ISBN/UID") or "").strip(),
            "rating": _rating(row.get("Star Rating")),
            "status": _status_from_shelf(row.get("Read Status")),
            "date_read": _parse_date(row.get("Last Date Read")),
        }
    return None


class _RateLimiter:
    """Process-wide courtesy limit for Open Library searches."""

    def __init__(self, requests_per_second: float = 3) -> None:
        self.interval = 1 / requests_per_second
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)


_OL_LIMITER = _RateLimiter()


def _search(provider, query: str):
    _OL_LIMITER.wait()
    return provider.search("book", query)


def _resolve_hit(provider, normalized: dict):
    """Best-effort Open Library match: ISBN first, then title and author."""
    isbn = (normalized.get("isbn") or "").replace("-", "").strip()
    queries = [isbn] if isbn else []
    title_query = " ".join(filter(None, [normalized.get("title"), normalized.get("author")]))
    if title_query:
        queries.append(title_query)

    for query in queries:
        try:
            results = _search(provider, query)
        except ProviderError:
            continue
        if results.hits:
            return results.hits[0]
    return None


def apply_match(user, review_entry: dict, hit) -> Book:
    """Apply one selected Open Library result using the original CSV values."""
    row = review_entry.get("row") or {}
    fmt = _detect_format(list(row))
    normalized = _normalize_row(row, fmt)
    if normalized is None:
        raise ValueError("unrecognized book row")

    item = services.upsert_item(_metadata_from_hit(hit))
    status = normalized.get("status")
    existing = Book.objects.filter(user=user, item=item).first()
    if existing and existing.status == Status.COMPLETED.value:
        status = None
    return services.update_tracking(
        user=user,
        item=item,
        score=normalized.get("rating"),
        status=status,
        end_date=(normalized.get("date_read") if status == Status.COMPLETED.value else None),
    )


def _metadata_from_hit(hit) -> Metadata:
    """Build sufficient book metadata from one Open Library search response."""
    raw = hit.raw or {}
    isbns = raw.get("isbn") or []
    return Metadata(
        source=hit.source,
        media_type=MediaType.BOOK.value,
        media_id=hit.media_id,
        title=hit.title,
        image=hit.image,
        release_date=date(hit.year, 1, 1) if hit.year else None,
        runtime=hit.runtime,
        max_progress=hit.runtime,
        raw={
            "search": raw,
            "edition": {"isbn_13": isbns[:1]} if isbns else {},
        },
    )


def import_csv(user, fobj: IO[str], *, job: ImportJob | None = None) -> ImportJob:
    if job is None:
        job = ImportJob.objects.create(user=user, kind="csv_books")
    job.start()

    reader = csv.DictReader(fobj)
    fmt = _detect_format(reader.fieldnames or [])
    if fmt == "unknown":
        job.fail("Couldn't detect CSV format — expected Goodreads or Storygraph headers.")
        return job

    rows = list(reader)
    job.total = len(rows)
    job.save(update_fields=["total"])

    needs_review: list[dict] = []
    provider = registry.get("openlibrary")
    normalized_rows = [(row, _normalize_row(row, fmt) or {}) for row in rows]

    # Only network matching runs concurrently. executor.map preserves CSV
    # order while prefetching later matches; ORM writes remain on this thread
    # so SQLite keeps its single-writer behavior and progress stays visible.
    def resolve(pair):
        _row, normalized = pair
        return _resolve_hit(provider, normalized) if normalized.get("title") else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for (row, normalized), hit in zip(
            normalized_rows,
            pool.map(resolve, normalized_rows),
            strict=True,
        ):
            if not normalized.get("title"):
                needs_review.append({"reason": "missing title", "row": row})
            elif hit is None:
                needs_review.append({"reason": "no Open Library match", "row": row})
            else:
                apply_match(user, {"row": row}, hit)

            job.processed += 1
            job.save_progress()

    job.finish(processed=len(rows), needs_review=needs_review)
    return job


def export_csv(user, fobj: IO[str]) -> None:
    """Storygraph-shaped export — closest to a de-facto standard."""
    from .csv_movies_shows import _safe_cell

    writer = csv.writer(fobj)
    writer.writerow([
        "Title", "Authors", "ISBN/UID", "Read Status",
        "Date Added", "Last Date Read", "Star Rating",
    ])
    status_map = {
        Status.COMPLETED.value: "read",
        Status.IN_PROGRESS.value: "currently reading",
        Status.PLANNING.value: "to read",
        Status.DROPPED.value: "did not finish",
        Status.PAUSED.value: "currently reading",
    }
    for b in Book.objects.filter(user=user).select_related("item"):
        title_only, _, author = (b.item.title or "").partition(" — ")
        rating = round(b.score / 2, 1) if b.score else ""
        isbn = ""
        if isinstance(b.item.raw, dict):
            edition = b.item.raw.get("edition") or {}
            for key in ("isbn_13", "isbn_10"):
                vals = edition.get(key) or []
                if vals:
                    isbn = vals[0]
                    break
        writer.writerow([
            _safe_cell(title_only),
            _safe_cell(author),
            isbn,
            status_map.get(b.status, ""),
            b.created_at.date().isoformat() if b.created_at else "",
            b.end_date.isoformat() if b.end_date else "",
            rating,
        ])
