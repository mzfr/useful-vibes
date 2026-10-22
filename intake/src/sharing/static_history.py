"""Render active history share links as a self-contained static site.

The generated tree is suitable for direct upload to Cloudflare Pages. It has
no dependency on Intake at request time: the root and unknown paths are blank,
and each active opaque token gets one expanded 30-day history page.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from django.template.loader import render_to_string
from django.utils import timezone

from core.collections import FILTERS, watchlist_items
from core.enums import MediaType
from core.models import Item
from tracking.models import CompletionEvent

from .models import AccessLink

STATIC_DIR = Path(__file__).with_name("static_site")
POSTER_HOSTS = {"image.tmdb.org", "covers.openlibrary.org"}
POSTER_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
POSTER_LIMIT = 5 * 1024 * 1024


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _poster_url(event: CompletionEvent) -> str:
    item = event.item
    if item.media_type == MediaType.EPISODE.value and item.parent_id and item.parent.parent_id:
        return item.parent.parent.image or item.image
    return item.image


def _poster_asset(url: str, cache: Path, output: Path) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in POSTER_HOSTS:
        return ""

    digest = hashlib.sha256(url.encode()).hexdigest()[:20]
    cache.mkdir(parents=True, exist_ok=True)
    cached = next(
        (cache / f"{digest}{suffix}" for suffix in POSTER_TYPES.values() if (cache / f"{digest}{suffix}").is_file()),
        None,
    )
    if cached is None:
        try:
            with httpx.stream("GET", url, timeout=10, follow_redirects=False) as response:
                response.raise_for_status()
                suffix = POSTER_TYPES.get(
                    response.headers.get("content-type", "").split(";", 1)[0].lower()
                )
                if suffix is None:
                    return ""
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > POSTER_LIMIT:
                        return ""
                    content.extend(chunk)
        except httpx.HTTPError:
            return ""
        if not content:
            return ""
        cached = cache / f"{digest}{suffix}"
        cached.write_bytes(content)

    target = output / "posters" / cached.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(cached, target)
    return f"/posters/{cached.name}"


def _poster_map(
    events: list[CompletionEvent], cache: Path, output: Path
) -> dict[str, str]:
    urls = {_poster_url(event) for event in events}
    return {url: _poster_asset(url, cache, output) for url in urls if url}


def _tmdb_url(item: Item) -> str:
    route = {MediaType.MOVIE.value: "movie", MediaType.TV.value: "tv"}.get(item.media_type)
    if (
        item.source == "tmdb" and route
        and item.media_id.isascii() and item.media_id.isdecimal()
    ):
        return f"https://www.themoviedb.org/{route}/{item.media_id}"
    return ""


def _history_context(
    link: AccessLink,
    events: list[CompletionEvent],
    start: date,
    end: date,
    posters: dict[str, str],
) -> dict:
    """Build one expanded calendar using the same grouping as local History."""
    from core.views import _history_cards

    by_day: dict[date, list[CompletionEvent]] = defaultdict(list)
    for event in events:
        day = timezone.localtime(event.completed_at).date()
        if start <= day <= end:
            by_day[day].append(event)

    grid_start = _monday(start)
    grid_end = _monday(end) + timedelta(days=6)
    days = []
    for offset in range((grid_end - grid_start).days + 1):
        day = grid_start + timedelta(days=offset)
        day_events = by_day[day]
        cards = []
        for card in _history_cards(day_events):
            item = card["show"]
            image = item.image
            cards.append({
                "title": item.title,
                "tmdb_url": _tmdb_url(item),
                "poster": posters.get(image, ""),
                "episode_code": card["episode_code"],
                "completion_count": card["completion_count"],
                "repeat_count": card["repeat_count"],
                "media_label": card["media_label"],
                "time": card["watched_at_time"],
            })
        days.append({
            "date": day,
            "watches": cards,
            "entry_count": len(day_events),
            "is_today": day == timezone.localdate(),
            "in_window": start <= day <= end,
        })

    runtime = sum(event.item.runtime or 0 for event in events)
    hours, minutes = divmod(runtime, 60)
    show_ids = {
        event.item.parent.parent_id
        for event in events
        if event.item.media_type == MediaType.EPISODE.value
        and event.item.parent_id and event.item.parent.parent_id
    }
    return {
        "label": link.label or "Watch history",
        "days": days,
        "range_label": f"{start:%d %b} – {end:%d %b %Y}",
        "entry_count": len(events),
        "runtime_label": f"{hours}h {minutes}m" if hours else f"{minutes}m",
        "movie_count": sum(event.item.media_type == MediaType.MOVIE.value for event in events),
        "show_count": len(show_ids),
        "episode_count": sum(event.item.media_type == MediaType.EPISODE.value for event in events),
        "latest": timezone.localtime(events[0].completed_at) if events else None,
    }


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _events_for(link: AccessLink, cutoff: date) -> list[CompletionEvent]:
    return list(
        CompletionEvent.objects.filter(user=link.owner, completed_at__date__gte=cutoff)
        .select_related("item", "item__parent", "item__parent__parent")
        .order_by("-completed_at", "-pk")
    )


def render_static_history(output: Path) -> dict[str, int]:
    """Replace ``output`` with a complete static site and return counts."""
    output = output.resolve()
    staging = output.with_name(f".{output.name}.tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    _write(staging / "index.html", STATIC_DIR.joinpath("blank.html").read_text())
    _write(staging / "404.html", STATIC_DIR.joinpath("blank.html").read_text())
    _write(staging / "robots.txt", "User-agent: *\nDisallow: /\n")
    _write(staging / "_headers", STATIC_DIR.joinpath("_headers").read_text())
    _write(staging / "history.css", STATIC_DIR.joinpath("history.css").read_text())
    _write(staging / "_worker.js", STATIC_DIR.joinpath("worker.mjs").read_text())

    active_links = [
        link
        for link in AccessLink.objects.select_related("owner")
        if link.scope == "history" and link.is_active()
    ]
    page_count = 0
    poster_cache = output.parent / ".history-share-poster-cache"
    cutoff = timezone.localdate() - timedelta(days=29)
    for link in active_links:
        events = _events_for(link, cutoff)
        posters = _poster_map(events, poster_cache, staging)

        current_html = render_to_string(
            "sharing/static_history.html",
            _history_context(link, events, cutoff, timezone.localdate(), posters),
        )
        private_root = staging / "__shares" / link.token
        _write(private_root / "index.html", current_html)
        items = watchlist_items(link.owner)
        watchlist_cards = [
            {
                "title": item.title,
                "tmdb_url": _tmdb_url(item),
                "poster": _poster_asset(item.image, poster_cache, staging),
                "media_type": item.media_type,
                "year": item.release_date.year if item.release_date else None,
            }
            for item in items
        ]
        for media_type, _label in FILTERS:
            cards = [
                card for card in watchlist_cards
                if media_type == "all" or card["media_type"] == media_type
            ]
            html = render_to_string(
                "sharing/static_watchlist.html",
                {"label": link.label, "cards": cards, "media_type": media_type, "filters": FILTERS},
            )
            directory = private_root / "watchlist"
            if media_type != "all":
                directory /= media_type
            _write(directory / "index.html", html)
            page_count += 1
        _write(
            private_root / "meta.json",
            json.dumps(
                {
                    "id": hashlib.sha256(link.token.encode()).hexdigest()[:16],
                    "label": link.label or "Watch history",
                    "expires_at": int(link.expires_at.timestamp())
                    if link.expires_at is not None
                    else None,
                },
                separators=(",", ":"),
            ),
        )
        page_count += 1

    if output.exists():
        shutil.rmtree(output)
    staging.rename(output)
    return {"links": len(active_links), "pages": page_count}
