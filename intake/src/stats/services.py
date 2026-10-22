"""
Stats aggregation — driven by CompletionEvent, which is the source of truth
for every watch/reread occurrence.

One query loads every event with pre-joined item facts for the selected
range; everything else is in-memory bucketing, same pattern as before (the
previous implementation issued one event-recovery query per completed row —
~9k queries on a 7k-event library — this loads once and buckets in Python,
consistent with that fix).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Count
from django.utils import timezone

from core.enums import MediaType
from core.formatting import humanize_minutes
from core.models import Item
from tracking.models import TV, Book, CompletionEvent, Episode, Movie

RANGES = [
    {"key": "7d", "label": "7 days"},
    {"key": "30d", "label": "30 days"},
    {"key": "90d", "label": "90 days"},
    {"key": "year", "label": "Year"},
    {"key": "all", "label": "All time"},
]

MEDIA_FILTERS = [
    {"key": "screen", "label": "Movies + TV"},
    {"key": "shows", "label": "TV"},
    {"key": "movies", "label": "Movies"},
    {"key": "books", "label": "Books"},
    {"key": "all", "label": "Everything"},
]

MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class TypeTotals:
    movies_completed: int = 0
    shows_completed: int = 0
    episodes_watched: int = 0
    books_finished: int = 0
    minutes_watched: int = 0
    pages_read: int = 0
    screen_events_missing_runtime: int = 0
    book_events_missing_pages: int = 0


@dataclass
class StatsPayload:
    range_key: str
    media_key: str
    year: int | None
    first_tracked: object | None  # date | None
    totals: TypeTotals
    tiles: list = field(default_factory=list)
    top_rated: list = field(default_factory=list)
    rated_count: int = 0
    average_score: float = 0.0
    score_histogram: dict[int, int] = field(default_factory=dict)
    score_bars: list = field(default_factory=list)
    rewatch_leaders: list = field(default_factory=list)
    top_genres: list = field(default_factory=list)
    decades: list = field(default_factory=list)
    heatmap: dict[str, int] = field(default_factory=dict)
    chart_year: int | None = None
    chart_year_count: int = 0
    month_bars: list = field(default_factory=list)
    available_years: list[int] = field(default_factory=list)
    heatmap_weeks: list = field(default_factory=list)
    heatmap_count: int = 0
    genre_trends: list = field(default_factory=list)


def _extract_genres(raw) -> list[str]:
    """Pull a flat list of genre names from a provider's raw payload.

    TMDB:           raw["genres"] = [{"id": ..., "name": "Action"}, ...]
    Open Library:   raw["work"]["subjects"] = ["Fantasy", "Adventure", ...]
    """
    if not isinstance(raw, dict):
        return []
    out: list[str] = []
    tmdb_genres = raw.get("genres")
    if isinstance(tmdb_genres, list):
        for g in tmdb_genres:
            if isinstance(g, dict):
                name = g.get("name", "")
            else:
                name = str(g)
            if name:
                out.append(name)
    work = raw.get("work")
    if isinstance(work, dict):
        for subject in work.get("subjects") or []:
            if isinstance(subject, str) and subject:
                out.append(subject)
    search = raw.get("search")
    if isinstance(search, dict):
        for subject in search.get("subject") or []:
            if isinstance(subject, str) and subject:
                out.append(subject)
    return list(dict.fromkeys(out))


def _media_matches(media_key: str, media_type: str) -> bool:
    return (
        media_key == "all"
        or (media_key == "screen" and media_type in (MediaType.EPISODE.value, MediaType.MOVIE.value))
        or (media_key == "shows" and media_type == MediaType.EPISODE.value)
        or (media_key == "movies" and media_type == MediaType.MOVIE.value)
        or (media_key == "books" and media_type == MediaType.BOOK.value)
    )


def _range_bounds(range_key: str, year: int | None) -> tuple[object, object] | None:
    """Return (start_date, end_date) inclusive, or None for "all time"."""
    today = timezone.localdate()
    if range_key == "7d":
        return today - timedelta(days=6), today
    if range_key == "30d":
        return today - timedelta(days=29), today
    if range_key == "90d":
        return today - timedelta(days=89), today
    if range_key == "year":
        y = year or today.year
        return date(y, 1, 1), date(y, 12, 31)
    return None  # "all"


def build_stats(
    user, range_key: str = "all", year: int | None = None, media_key: str = "all",
) -> StatsPayload:
    if range_key not in {r["key"] for r in RANGES}:
        range_key = "all"
    # Preserve the service's original API: callers passing only `year=` expect
    # that year to be the selected range.
    if year is not None and range_key == "all":
        range_key = "year"
    if media_key not in {m["key"] for m in MEDIA_FILTERS}:
        media_key = "all"

    totals = TypeTotals()
    bounds = _range_bounds(range_key, year)

    # One flat query for every event with pre-joined item facts.
    all_rows = list(
        CompletionEvent.objects.filter(user=user).values(
            "id",
            "completed_at",
            "item_id",
            "item__media_type",
            "item__runtime",
            "item__max_progress",
            "item__release_date",
            "item__parent_id",
            "item__parent__parent_id",
        )
    )

    filtered_all_rows = [
        ev for ev in all_rows if _media_matches(media_key, ev["item__media_type"])
    ]
    first_tracked = None
    if filtered_all_rows:
        first_tracked = min(
            timezone.localtime(r["completed_at"]).date() for r in filtered_all_rows
        )

    scoped_rows = []
    for ev in filtered_all_rows:
        local = timezone.localtime(ev["completed_at"])
        if bounds is not None and not (bounds[0] <= local.date() <= bounds[1]):
            continue
        scoped_rows.append((ev, local))

    # CompletionEvent is the source of truth: every completion, including a
    # rewatch/reread, contributes once in every range.
    movie_items, episode_items, book_items = set(), set(), set()
    for ev, _ in scoped_rows:
        mt = ev["item__media_type"]
        if mt == MediaType.MOVIE.value:
            movie_items.add(ev["item_id"])
        elif mt == MediaType.EPISODE.value:
            episode_items.add(ev["item_id"])
        elif mt == MediaType.BOOK.value:
            book_items.add(ev["item_id"])

    totals.movies_completed = sum(
        1 for ev, _ in scoped_rows if ev["item__media_type"] == MediaType.MOVIE.value
    )
    totals.episodes_watched = sum(
        1 for ev, _ in scoped_rows if ev["item__media_type"] == MediaType.EPISODE.value
    )
    totals.books_finished = sum(
        1 for ev, _ in scoped_rows if ev["item__media_type"] == MediaType.BOOK.value
    )

    show_ids = {
        ev["item__parent__parent_id"]
        for ev, _ in scoped_rows
        if ev["item__media_type"] == MediaType.EPISODE.value
        and ev["item__parent__parent_id"] is not None
    }
    totals.shows_completed = len(show_ids)

    screen_event_count = 0
    for ev, _ in scoped_rows:
        if ev["item__media_type"] in (MediaType.MOVIE.value, MediaType.EPISODE.value):
            screen_event_count += 1
            if ev["item__runtime"] is None:
                totals.screen_events_missing_runtime += 1
            else:
                totals.minutes_watched += ev["item__runtime"]
        elif ev["item__media_type"] == MediaType.BOOK.value:
            if ev["item__max_progress"] is None:
                totals.book_events_missing_pages += 1
            else:
                totals.pages_read += ev["item__max_progress"]

    book_sub = f"{totals.pages_read} known pages" if totals.pages_read else ""
    if totals.book_events_missing_pages:
        missing = totals.book_events_missing_pages
        book_sub = f"{book_sub} · {missing} missing page count" if book_sub else "Page count unavailable"

    watch_time_sub = ""
    if totals.screen_events_missing_runtime:
        missing = totals.screen_events_missing_runtime
        watch_time_sub = f"{missing} completion{'s' if missing != 1 else ''} missing runtime"
    tiles = [
        {"key": "Episodes", "value": totals.episodes_watched, "sub": f"{totals.shows_completed} shows"},
        {"key": "Movies", "value": totals.movies_completed, "sub": ""},
        {"key": "Books", "value": totals.books_finished, "sub": book_sub},
    ]
    if screen_event_count:
        tiles.append({
            "key": "Watch time",
            "value": humanize_minutes(totals.minutes_watched) if totals.minutes_watched else "Unknown",
            "sub": watch_time_sub,
        })
    tiles = [t for t in tiles if t["value"]]

    # Current ratings for items completed in the selected range.
    movies = Movie.objects.filter(user=user, item_id__in=movie_items).select_related("item")
    shows = TV.objects.filter(user=user, item_id__in=show_ids).select_related("item")
    books = Book.objects.filter(user=user, item_id__in=book_items).select_related("item")
    episodes = Episode.objects.filter(user=user, item_id__in=episode_items).select_related("item")

    rated_rows: list = []
    for qs in (movies, shows, books, episodes):
        rated_rows += list(qs.filter(score__isnull=False).order_by("-score")[:10])
    top_rated = sorted(rated_rows, key=lambda r: r.score or 0, reverse=True)[:10]

    # Score histogram — one small grouped query per model, scoped to the
    # items actually watched in this range.
    hist: Counter[int] = Counter()
    for qs in (movies, shows, books, episodes):
        for row in qs.filter(score__isnull=False).values("score").annotate(n=Count("id")):
            hist[row["score"]] += row["n"]
    score_histogram = {k: hist.get(k, 0) for k in range(1, 11)}
    rated_count = sum(score_histogram.values())
    average_score = round(sum(k * v for k, v in score_histogram.items()) / rated_count, 1) if rated_count else 0.0
    peak_score_count = max(score_histogram.values(), default=0)
    score_bars = [
        {"score": k, "count": v, "percent": round(100 * v / peak_score_count) if peak_score_count else 0}
        for k, v in score_histogram.items()
    ]

    # A completion is a rewatch/reread when an earlier event for that item
    # exists, even if that first event sits outside the selected range.
    first_event_ids: dict[int, int] = {}
    for ev in sorted(filtered_all_rows, key=lambda row: (row["completed_at"], row["id"])):
        first_event_ids.setdefault(ev["item_id"], ev["id"])
    rewatch_counts: Counter[int] = Counter(
        ev["item_id"]
        for ev, _ in scoped_rows
        if ev["id"] != first_event_ids[ev["item_id"]]
    )

    class RewatchRow:
        def __init__(self, item, repeats):
            self.item = item
            self.repeats = repeats

    rewatch_leaders = []
    if rewatch_counts:
        top_items = {
            item.pk: item
            for item in Item.objects.filter(pk__in=[i for i, _count in rewatch_counts.most_common(10)])
        }
        rewatch_leaders = [
            RewatchRow(top_items[item_id], count)
            for item_id, count in rewatch_counts.most_common(10)
            if item_id in top_items
        ]

    # Catalog facts for genre/decade summaries and the selected year's genre
    # trend. Episodes inherit genres from their parent show.
    def activity_item_id(ev):
        if ev["item__media_type"] == MediaType.EPISODE.value:
            return ev["item__parent__parent_id"]
        return ev["item_id"]

    all_activity_ids = {
        activity_item_id(ev) for ev in filtered_all_rows if activity_item_id(ev) is not None
    }
    activity_items = {
        item.pk: item
        for item in Item.objects.filter(pk__in=all_activity_ids).only("id", "raw", "release_date")
    }
    item_genres = {
        item_id: _extract_genres(item.raw) for item_id, item in activity_items.items()
    }

    genre_counter: Counter[str] = Counter()
    decade_counter: Counter[int] = Counter()
    scoped_activity_ids = {
        activity_item_id(ev) for ev, _local in scoped_rows if activity_item_id(ev) is not None
    }
    for item_id in scoped_activity_ids:
        item = activity_items.get(item_id)
        if item is None:
            continue
        genre_counter.update(item_genres.get(item_id, []))
        if item.release_date:
            decade_counter[(item.release_date.year // 10) * 10] += 1
    top_genres = sorted(genre_counter.items(), key=lambda row: (-row[1], row[0].casefold()))[:10]
    decades = sorted(decade_counter.items())

    # Range-scoped event counts by local calendar date.
    heatmap: dict[str, int] = {}
    for _ev, local in scoped_rows:
        key = local.date().isoformat()
        heatmap[key] = heatmap.get(key, 0) + 1

    # "Watched by month" always covers one calendar year — the range being
    # viewed if it's "year", otherwise the current year — bucketed from the
    # already-loaded all_rows so this doesn't re-query.
    available_years = sorted(
        {timezone.localtime(ev["completed_at"]).year for ev in filtered_all_rows},
        reverse=True,
    )
    chart_year = year if range_key == "year" and year is not None else timezone.localdate().year
    month_counts = [0] * 12
    year_day_counts: Counter[date] = Counter()
    genre_months: dict[str, list[int]] = {}
    for ev in filtered_all_rows:
        local = timezone.localtime(ev["completed_at"])
        if local.year != chart_year:
            continue
        month_counts[local.month - 1] += 1
        year_day_counts[local.date()] += 1
        for genre in item_genres.get(activity_item_id(ev), []):
            genre_months.setdefault(genre, [0] * 12)[local.month - 1] += 1

    chart_year_count = sum(month_counts)
    peak_month = max(month_counts, default=0)
    month_bars = [
        {
            "label": MONTH_LABELS[i],
            "count": month_counts[i],
            "percent": round(100 * month_counts[i] / peak_month) if peak_month else 0,
        }
        for i in range(12)
    ]

    # GitHub-style calendar columns, Monday → Sunday. Empty padding cells let
    # old years and the current year use the same compact grid.
    first_day = date(chart_year, 1, 1)
    last_day = date(chart_year, 12, 31)
    grid_start = first_day - timedelta(days=first_day.weekday())
    grid_end = last_day + timedelta(days=6 - last_day.weekday())
    peak_day = max(year_day_counts.values(), default=0)
    heatmap_weeks = []
    cursor = grid_start
    while cursor <= grid_end:
        week = []
        for _ in range(7):
            in_year = cursor.year == chart_year
            count = year_day_counts.get(cursor, 0) if in_year else 0
            level = min(3, max(1, round(3 * count / peak_day))) if count and peak_day else 0
            week.append({
                "date": cursor,
                "count": count,
                "level": level,
                "in_year": in_year,
            })
            cursor += timedelta(days=1)
        heatmap_weeks.append(week)

    # Top five yearly genres, normalized independently so the tiny trend bars
    # remain readable even when one genre dominates absolute totals.
    genre_trends = []
    for genre, counts in sorted(
        genre_months.items(), key=lambda row: sum(row[1]), reverse=True
    )[:5]:
        peak = max(counts, default=0)
        genre_trends.append({
            "name": genre,
            "total": sum(counts),
            "months": [
                {
                    "label": MONTH_LABELS[i],
                    "count": count,
                    "percent": round(100 * count / peak) if peak else 0,
                }
                for i, count in enumerate(counts)
            ],
        })

    return StatsPayload(
        range_key=range_key,
        media_key=media_key,
        year=year,
        first_tracked=first_tracked,
        totals=totals,
        tiles=tiles,
        top_rated=top_rated,
        rated_count=rated_count,
        average_score=average_score,
        score_histogram=score_histogram,
        score_bars=score_bars,
        rewatch_leaders=rewatch_leaders,
        top_genres=top_genres,
        decades=decades,
        heatmap=heatmap,
        chart_year=chart_year,
        chart_year_count=chart_year_count,
        month_bars=month_bars,
        available_years=available_years,
        heatmap_weeks=heatmap_weeks,
        heatmap_count=sum(year_day_counts.values()),
        genre_trends=genre_trends,
    )
