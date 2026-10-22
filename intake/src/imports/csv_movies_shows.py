"""
CSV importer for movies + shows in Trakt's history export shape.

Trakt's export columns (CSV):

  rated_at, title, year, trakt_rating, imdb_id, tmdb_id, type, season, episode

We accept either history.csv or ratings.csv variants — the columns are a superset.
Unknown rows go into `needs_review`.
"""
from __future__ import annotations

import csv
from datetime import datetime
from typing import IO

from core.enums import MediaType, Status
from providers import registry
from providers.base import ProviderError
from tracking import services

from .models import ImportJob

# What "type" values Trakt uses. We support all four.
_TRAKT_TYPES = {"movie", "show", "season", "episode"}


def _parse_score(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    if v < 1 or v > 10:
        return None
    return v


def _parse_date(raw: str | None):
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def import_csv(user, fobj: IO[str], *, job: ImportJob | None = None) -> ImportJob:
    """Run a Trakt-shaped CSV import synchronously, updating `job` as we go."""
    if job is None:
        job = ImportJob.objects.create(user=user, kind="csv_movies_shows")

    job.start()

    try:
        reader = csv.DictReader(fobj)
        rows = list(reader)
    except Exception as exc:
        job.fail(f"CSV parse error: {exc}")
        return job

    job.total = len(rows)
    job.save(update_fields=["total"])

    needs_review: list[dict] = []
    try:
        provider = registry.get("tmdb")
    except (ProviderError, RuntimeError, ValueError) as exc:
        job.fail(f"TMDB provider unavailable: {exc}")
        return job

    def history_key(row):
        return (
            (row.get("type") or "").strip().lower(),
            (row.get("tmdb_id") or "").strip(),
            row.get("season") or "",
            row.get("episode") or "",
        )

    # Chronological order makes each duplicate history row a real rewatch
    # snapshot instead of repeatedly overwriting one current row.
    rows.sort(key=lambda row: (history_key(row), row.get("watched_at") or row.get("rated_at") or ""))
    play_counts: dict[tuple, int] = {}
    for row in rows:
        kind = (row.get("type") or "").strip().lower()
        tmdb_id = (row.get("tmdb_id") or "").strip()
        score = _parse_score(row.get("trakt_rating"))
        ts = _parse_date(row.get("rated_at") or row.get("watched_at"))

        if kind not in _TRAKT_TYPES or not tmdb_id:
            needs_review.append({"reason": "unknown row", "row": row})
            job.processed += 1
            job.save(update_fields=["processed"])
            continue

        try:
            if kind == "movie":
                md = provider.details(MediaType.MOVIE.value, tmdb_id)
                item = services.upsert_item(md)
                tracked = services.update_tracking(
                    user=user, item=item,
                    score=score, status=Status.COMPLETED.value,
                    end_date=ts.date() if ts else None,
                )
                key = history_key(row)
                play_counts[key] = play_counts.get(key, 0) + 1
                if play_counts[key] > 1:
                    tracked.repeats = play_counts[key] - 1
                    tracked.end_date = ts.date() if ts else tracked.end_date
                    tracked.save()
            elif kind == "show":
                md = provider.details(MediaType.TV.value, tmdb_id)
                item = services.upsert_item(md)
                services.update_tracking(
                    user=user, item=item, score=score,
                )
            elif kind == "season":
                try:
                    sn = int(row.get("season", ""))
                except (TypeError, ValueError):
                    needs_review.append({"reason": "bad season number", "row": row})
                    continue
                season_item = services.materialize_season(provider, tmdb_id, sn)
                services.update_tracking(user=user, item=season_item, score=score)
            elif kind == "episode":
                try:
                    sn = int(row.get("season", ""))
                    en = int(row.get("episode", ""))
                except (TypeError, ValueError):
                    needs_review.append({"reason": "bad ep numbers", "row": row})
                    continue
                season_item = services.materialize_season(provider, tmdb_id, sn)
                # Find the specific episode Item.
                from core.models import Item
                ep_item = Item.objects.filter(
                    parent=season_item, media_type=MediaType.EPISODE.value,
                    episode_number=en,
                ).first()
                if ep_item is None:
                    needs_review.append({"reason": "episode not found in season", "row": row})
                    continue
                tracked = services.update_tracking(
                    user=user, item=ep_item,
                    score=score, status=Status.COMPLETED.value,
                    end_date=ts.date() if ts else None,
                )
                key = history_key(row)
                play_counts[key] = play_counts.get(key, 0) + 1
                if play_counts[key] > 1:
                    tracked.repeats = play_counts[key] - 1
                    tracked.end_date = ts.date() if ts else tracked.end_date
                    tracked.save()
        except ProviderError as exc:
            needs_review.append({"reason": f"provider error: {exc}", "row": row})

        job.processed += 1
        # Save periodically so the polling UI sees progress.
        job.save_progress()

    job.finish(processed=len(rows), needs_review=needs_review)
    return job


def _safe_cell(value):
    """Neutralize spreadsheet formula injection (=, +, -, @ prefixes).

    Titles come from external providers; `=HYPERLINK(...)` in a title must not
    execute when the export is opened in Excel/Sheets.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _event_dates_for(rows, user):
    """Batch-load per-item completion dates from CompletionEvent (source of truth).

    Returns {item_id: [local dates]} for the given tracking rows. One query.
    """
    from collections import defaultdict

    from django.utils import timezone

    from tracking.models import CompletionEvent

    item_ids = [r.item_id for r in rows]
    out: dict[int, list] = defaultdict(list)
    events = (
        CompletionEvent.objects.filter(user=user, item_id__in=item_ids)
        .order_by("completed_at", "pk")
        .values_list("item_id", "completed_at")
    )
    for item_id, completed_at in events:
        out[item_id].append(timezone.localtime(completed_at).date())
    return out


def export_csv(user, fobj: IO[str]) -> None:
    """Write a Trakt-compatible CSV of all movie + episode completions.

    Dates come from CompletionEvent (the source of truth), batch-loaded so the
    whole export costs a constant handful of queries regardless of library size.
    """
    from tracking.models import Episode, Movie

    writer = csv.writer(fobj)
    writer.writerow(
        ["rated_at", "title", "year", "trakt_rating", "tmdb_id", "type", "season", "episode"]
    )

    movies = list(
        Movie.objects.filter(user=user, status=Status.COMPLETED.value)
        .select_related("item")
    )
    movie_dates = _event_dates_for(movies, user)
    for m in movies:
        for completed_at in movie_dates.get(m.item_id) or _fallback_dates(m):
            writer.writerow([
                completed_at.isoformat(),
                _safe_cell(m.item.title),
                m.item.release_date.year if m.item.release_date else "",
                m.score or "",
                m.item.media_id,
                "movie",
                "",
                "",
            ])

    episodes = list(
        Episode.objects.filter(user=user, status=Status.COMPLETED.value)
        .select_related("item", "item__parent", "item__parent__parent")
    )
    ep_dates = _event_dates_for(episodes, user)
    for e in episodes:
        show = e.item.parent.parent if e.item.parent else None
        if show is None:
            continue
        for completed_at in ep_dates.get(e.item_id) or _fallback_dates(e):
            writer.writerow([
                completed_at.isoformat(),
                _safe_cell(show.title),
                show.release_date.year if show.release_date else "",
                e.score or "",
                show.media_id,
                "episode",
                e.item.season_number or "",
                e.item.episode_number or "",
            ])


def _fallback_dates(row):
    """Used only when no completion events exist for a row."""
    return [row.end_date] if row.end_date else []
