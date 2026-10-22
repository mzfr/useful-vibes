import re
import unicodedata
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from core.enums import MediaType
from core.formatting import humanize_minutes
from releases.services import build_feed
from tracking.models import CompletionEvent
from tracking.up_next import in_progress_movies, shows_for_user

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# "683" — an all-numeric query goes straight to a TMDB ID lookup.
_DIGITS = re.compile(r"^\d{1,10}$")


def _normalized_search_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _search_score(query: str, title: str) -> int:
    if query == title:
        return 1000
    if title.startswith(query):
        return 900 - len(title)
    if any(word.startswith(query) for word in title.split()):
        return 850 - len(title)
    position = title.find(query)
    if position >= 0:
        return 800 - position
    if len(query) < 3:
        return 0
    query_trigrams = {query[i:i + 3] for i in range(len(query) - 2)}
    title_trigrams = {title[i:i + 3] for i in range(len(title) - 2)}
    similarity = 2 * len(query_trigrams & title_trigrams) / (
        len(query_trigrams) + len(title_trigrams)
    )
    return round(similarity * 700) if similarity >= 0.3 else 0


SEARCHABLE_TYPES = [MediaType.MOVIE.value, MediaType.TV.value]
SEARCH_FILTERS = [("all", "All"), (MediaType.MOVIE.value, "Movies"), (MediaType.TV.value, "Shows")]

# Old titles crowd out what you actually meant ("searchi" the show vs. a 1950s
# western), so results default to 2000+; the search bar's checkbox lifts it.
YEAR_FILTER_DEFAULT = 2000


def _min_year(request) -> int:
    """Oldest release year to include; 0 means show everything.

    The search form always submits an explicit value — a hidden reset input
    ("off") followed by the checkbox ("on" when checked). The checkbox, being
    last, wins when present. A request with neither (e.g. a bookmarked plain
    URL) defaults the filter on.
    """
    values = request.GET.getlist("year_filter")
    if values and values[-1] == "off":
        return 0
    return YEAR_FILTER_DEFAULT


def _local_search(query: str, media_type: str, min_year: int = 0) -> tuple[list[dict], int]:
    """Rank local root items by exact, prefix, substring, then trigram similarity.

    An exact media_id match (pasted TMDB ID of something already tracked)
    scores above everything. Items released before `min_year` are hidden
    (0 = no year limit). Returns the ranked hits plus how many were hidden.
    """
    from core.models import Item

    q = _normalized_search_text(query.strip())
    if len(q) < 2:
        return [], 0

    # Books have their own search at /books/search; this box is movies + shows.
    qs = Item.objects.filter(
        season_number__isnull=True,
        episode_number__isnull=True,
        media_type__in=SEARCHABLE_TYPES,
    )
    if media_type in SEARCHABLE_TYPES:
        qs = qs.filter(media_type=media_type)

    ranked = []
    hidden = 0
    for item in qs.only(
        "id", "source", "media_type", "media_id", "title", "image", "release_date"
    ):
        score = _search_score(q, _normalized_search_text(item.title))
        if not score and item.media_id == q:
            score = 1000
        if not score:
            continue
        year = item.release_date.year if item.release_date else None
        if min_year and year is not None and year < min_year:
            hidden += 1
            continue
        ranked.append((score, item.title.casefold(), item, year))

    hits = []
    for _score, _title, item, year in sorted(ranked, key=lambda row: (-row[0], row[1]))[:40]:
        hits.append({
            "source": item.source,
            "media_type": item.media_type,
            "media_id": item.media_id,
            "score": _score,
            "url": item.get_absolute_url(),
            "title": item.title,
            "image": item.image,
            "year": year,
        })
    return hits, hidden


def _recent_history(user, limit=6):
    """Return the most recent completed watches/reads for the history shelf."""
    events = (
        CompletionEvent.objects.filter(user=user)
        .select_related("item", "item__parent")
        .order_by("-completed_at")[:limit]
    )
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    rows = []
    for ev in events:
        item = ev.item
        # For episodes, link to the show; for movies/books, link directly.
        if item.media_type == MediaType.EPISODE.value:
            show = item.parent.parent if item.parent and item.parent.parent else None
            label_item = show or item
            episode_code = f"S{item.season_number:02d}E{item.episode_number:02d}" if item.season_number is not None else ""
        else:
            label_item = item
            episode_code = ""

        d = ev.completed_at.date() if hasattr(ev.completed_at, "date") else ev.completed_at
        if d == today:
            watched_label = "Today"
        elif d == yesterday:
            watched_label = "Yesterday"
        else:
            watched_label = d.strftime("%-d %b")

        rows.append({
            "item": label_item,
            "episode_code": episode_code,
            "media_label": {
                MediaType.EPISODE.value: "EP",
                MediaType.MOVIE.value: "MOVIE",
                MediaType.BOOK.value: "BOOK",
            }.get(item.media_type, item.media_type.upper()),
            "watched_at_label": watched_label,
        })
    return rows


@login_required
def home(request):
    query = request.GET.get("q", "").strip()
    media_type = request.GET.get("type", "")
    min_year = _min_year(request)

    local_results, hidden_local = _local_search(query, media_type, min_year) if query else ([], 0)
    year_filter_off = min_year == 0

    # Only a *strong* local match (substring-or-better) counts as "found".
    # A weak trigram hit ("The O.C." for "The Odyssey") must not hide the
    # TMDB fallback — searching for something unreleased is the whole point.
    found_locally = bool(local_results) and local_results[0]["score"] >= 800

    if request.headers.get("HX-Request"):
        # Live search targets #search-results — return just that block,
        # otherwise the whole page gets nested inside it.
        return render(request, "core/partials/_search_results.html", {
            "query": query,
            "media_type": media_type,
            "local_results": local_results,
            "hidden_local": hidden_local,
            "year_filter_off": year_filter_off,
            "search_remote": bool(query) and not found_locally,
        })

    feed = build_feed(request.user)

    # History shelf — recent completions.
    history = _recent_history(request.user, limit=14)
    month_ago = timezone.now() - timedelta(days=30)
    history_count = CompletionEvent.objects.filter(
        user=request.user, completed_at__gte=month_ago
    ).count()

    show_rows = shows_for_user(request.user)
    movie_rows = in_progress_movies(request.user)
    episode_count = sum(r.unwatched_count or 1 for r in show_rows)

    return render(
        request,
        "core/home.html",
        {
            # Up Next
            "show_rows": show_rows,
            "movie_rows": movie_rows,
            "episode_count": episode_count,
            # History
            "history": history,
            "history_count": history_count,
            # Search
            "query": query,
            "media_type": media_type,
            "media_types": SEARCH_FILTERS,
            "local_results": local_results,
            "hidden_local": hidden_local,
            "year_filter_off": year_filter_off,
            # Only reach for TMDB when the library came up empty — that's the
            # "saw a trailer, it isn't out yet" case.
            "search_remote": bool(query) and not found_locally,
            # Releases
            **feed,
        },
    )


@login_required
def remote_search(request):
    """TMDB fallback for the home search box, lazily loaded via HTMX.

    Only reached when the local catalog had no match, so a normal library
    lookup never waits on the network.

    An all-numeric query is also resolved as a TMDB ID (movie and TV have
    independent ID namespaces, so both requested types are tried). A direct
    details lookup finds titles that text search buries past its first page,
    and the match is pinned above the text results.
    """
    from providers import registry
    from providers.base import NotSupported, ProviderError, SearchHit

    query = request.GET.get("q", "").strip()
    media_type = request.GET.get("type", "")
    types = [media_type] if media_type in SEARCHABLE_TYPES else SEARCHABLE_TYPES
    min_year = _min_year(request)

    hits, error, id_hit = [], None, None
    if query:
        try:
            provider = registry.get("tmdb")
            if _DIGITS.match(query):
                id_hit = _resolve_id_hit(provider, query, types)
            for key in types:
                results = provider.search(key, query)
                hits.extend(results.hits[:20])
        except (NotSupported, ProviderError) as exc:
            error = str(exc)

    # Interleaving by popularity isn't available on SearchHit, so keep TMDB's
    # per-type relevance order and put exact title matches first.
    q = _normalized_search_text(query)
    hits.sort(key=lambda h: -_search_score(q, _normalized_search_text(h.title)))

    if id_hit is not None:
        # The pasted ID is the user's explicit pick; drop the duplicate a text
        # search may also have returned rather than showing it twice.
        hits = [
            h for h in hits
            if (h.media_type, str(h.media_id)) != (id_hit.media_type, str(id_hit.media_id))
        ]

    hidden_count = 0
    if min_year:
        # Title-only rows (no year parsed) stay visible — an empty year isn't
        # proof of age, and hiding them could hide the one wanted result.
        kept = [h for h in hits if h.year is None or h.year >= min_year]
        hidden_count = len(hits) - len(kept)
        hits = kept

    return render(request, "core/partials/_remote_results.html", {
        "query": query,
        "hits": hits[:24],
        "id_hit": id_hit,
        "hidden_count": hidden_count,
        "year_filter_off": min_year == 0,
        "error": error,
    })


def _resolve_id_hit(provider, media_id: str, types: list[str]):
    """Resolve an all-numeric query as a TMDB movie/show ID, or None.

    A failed lookup in one namespace just means "not that type" — 404s are
    expected here, so any ProviderError moves the probe to the next type.
    """
    from providers.base import ProviderError, SearchHit

    for media_type in types:
        try:
            metadata = provider.details(media_type, media_id)
        except ProviderError:
            continue
        return SearchHit(
            source="tmdb",
            media_type=media_type,
            media_id=str(metadata.media_id),
            title=metadata.title,
            image=metadata.image,
            year=metadata.release_date.year if metadata.release_date else None,
            synopsis=metadata.synopsis,
        )
    return None


# ---------------------------------------------------------------------------
# History page (5a)
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _year_strip(user, year: int, current_monday: date) -> dict:
    """52 buckets, each with a 0-3 density level for --heat-N."""
    counts = (
        CompletionEvent.objects.filter(
            user=user, completed_at__year=year,
        )
        .annotate(d=TruncDate("completed_at"))
        .values("d")
        .annotate(n=Count("id"))
    )
    weeks: dict[int, int] = {}
    for row in counts:
        wk = row["d"].isocalendar()[1]
        weeks[wk] = weeks.get(wk, 0) + row["n"]

    peak = max(weeks.values(), default=0)
    out = []
    for wk in range(1, 53):
        n = weeks.get(wk, 0)
        level = 0 if not n else 1 + min(int(n / max(peak, 1) * 3), 2)
        start = date.fromisocalendar(year, wk, 1)
        out.append({
            "start": start,
            "count": n,
            "level": level,
            "label": f"Week {wk}",
            "is_current": start == current_monday,
        })
    return {"year_weeks": out, "year_count": sum(weeks.values()), "strip_year": year}


def _history_cards(events):
    """Group a day's episodes by show and season, newest activity first."""
    groups = {}
    for event in events:
        item = event.item
        show = item.parent.parent if item.parent and item.parent.parent else item
        can_group = (
            item.media_type == MediaType.EPISODE.value
            and show != item
            and item.season_number is not None
            and item.episode_number is not None
        )
        key = ("season", show.pk, item.season_number) if can_group else ("event", event.pk)
        groups.setdefault(key, []).append(event)

    cards = []
    for key, group in groups.items():
        item = group[0].item
        show = item
        episode_code = ""
        repeat_count = 0
        if item.media_type == MediaType.EPISODE.value:
            show = item.parent.parent if item.parent and item.parent.parent else item
            if key[0] == "season":
                numbers = sorted({event.item.episode_number for event in group})
                runs = []
                for number in numbers:
                    if runs and number == runs[-1][-1] + 1:
                        runs[-1].append(number)
                    else:
                        runs.append([number])
                ranges = ", ".join(
                    f"E{run[0]}–{run[-1]}" if len(run) > 1 else f"E{run[0]}"
                    for run in runs
                )
                episode_code = f"S{item.season_number} · {ranges}"
                repeat_count = len(group) - len(numbers)
            else:
                season = f"S{item.season_number}" if item.season_number is not None else "Season ?"
                episode = f"E{item.episode_number}" if item.episode_number is not None else "Episode ?"
                episode_code = f"{season} · {episode}"
        times = [timezone.localtime(event.completed_at) for event in group]
        first, last = min(times).strftime("%H:%M"), max(times).strftime("%H:%M")
        cards.append({
            "show": show,
            "episode_code": episode_code,
            "completion_count": len(group),
            "repeat_count": repeat_count,
            "media_label": {
                MediaType.EPISODE.value: "EP",
                MediaType.MOVIE.value: "MOVIE",
                MediaType.BOOK.value: "BOOK",
            }.get(item.media_type, item.media_type.upper()),
            "watched_at_time": first if first == last else f"{first}–{last}",
        })
    return cards


@login_required
def history(request):
    today = timezone.localdate()
    view = request.GET.get("view", "month")
    if view not in {"week", "month"}:
        view = "month"

    # ?start= wins; else ?month=&year= jumps to the 1st of that month.
    start = _parse_date(request.GET.get("start", "")) or today
    if request.GET.get("month"):
        try:
            start = date(
                int(request.GET.get("year", today.year)),
                int(request.GET["month"]), 1,
            )
        except (ValueError, TypeError):
            start = today
    display_month, display_year = start.month, start.year

    if view == "month":
        month_start = date(display_year, display_month, 1)
        next_month = (
            date(display_year + 1, 1, 1)
            if display_month == 12 else date(display_year, display_month + 1, 1)
        )
        period_start = month_start - timedelta(days=month_start.weekday())
        month_end = next_month - timedelta(days=1)
        period_end = month_end + timedelta(days=6 - month_end.weekday())
    else:
        period_start = start - timedelta(days=start.weekday())
        period_end = period_start + timedelta(days=6)

    watches = list(
        CompletionEvent.objects.filter(
            user=request.user,
            completed_at__date__range=(period_start, period_end),
        )
        .select_related("item", "item__parent", "item__parent__parent")
        .order_by("-completed_at")
    )

    by_day: dict[date, list] = {}
    for w in watches:
        d = timezone.localtime(w.completed_at).date()
        by_day.setdefault(d, []).append(w)

    days = []
    day_count = (period_end - period_start).days + 1
    for i in range(day_count):
        day_date = period_start + timedelta(days=i)
        day_watches = by_day.get(day_date, [])
        days.append({
            "date": day_date,
            "watches": _history_cards(day_watches),
            "completion_count": len(day_watches),
            "is_today": day_date == today,
            "in_month": day_date.month == display_month,
        })

    episode_count = len(watches)
    runtime_total = sum(
        (ev.item.runtime or 0) for ev in watches
    )

    if view == "month":
        range_label = f"{MONTHS[display_month - 1]} {display_year}"
        prev_date = month_start - timedelta(days=1)
        next_date = next_month
    else:
        # "27 – 2 Aug 2026" is wrong when the week spans months.
        range_label = (
            f"{period_start.day} – {period_end.day} {MONTHS[period_start.month - 1]} {period_end.year}"
            if period_start.month == period_end.month
            else f"{period_start.day} {MONTHS[period_start.month - 1]} – {period_end.day} {MONTHS[period_end.month - 1]} {period_end.year}"
        )
        prev_date = period_start - timedelta(days=7)
        next_date = period_start + timedelta(days=7)

    return render(request, "core/history.html", {
        "days": days,
        "week_start": period_start,
        "year": display_year,
        "month_label": MONTHS[display_month - 1],
        "range_label": range_label,
        "episode_count": episode_count,
        "runtime_label": humanize_minutes(runtime_total, zero="0 min"),
        "view": view,
        "prev_start": prev_date,
        "next_start": next_date,
        "showing_today": period_start <= today <= period_end,
        "month_options": [
            {"number": i + 1, "label": m, "active": i + 1 == display_month}
            for i, m in enumerate(MONTHS)
        ],
        "views": [
            {"label": "Week", "url": f"?view=week&start={start.isoformat()}", "active": view == "week"},
            {"label": "Month", "url": f"?view=month&month={display_month}&year={display_year}", "active": view == "month"},
        ],
        **_year_strip(request.user, period_start.year, period_start),
    })
