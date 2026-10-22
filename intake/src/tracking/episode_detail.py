"""
Single-episode detail page — still/backdrop, overview, prev/next nav, watch
history, rating, and a private note. One click from Up Next or the season
rail lands here on the exact next episode, instead of show → seasons →
episode grid → row.

Read side (`build_episode_context`) is shared by the full page and every
partial re-render, same split as show_detail.py / season_views.py. Write
views reuse `_mark_episode_watched` / `_unwatch_episode_item` from
season_views so there is still exactly one code path for "mark watched".
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from core.enums import MediaType
from core.models import Favorite, Item

from . import services
from .models import CompletionEvent, Episode
from .season_views import (
    _build_rows,
    _load_season_item,
    _mark_episode_watched,
    _parse_watched_on,
    _unwatch_episode_item,
    _watched_at_label,
)
from .show_detail import _season_summaries


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} watch"


def _episode_item_or_404(source: str, media_id: str, season_number: int, episode_number: int) -> Item:
    return get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.EPISODE.value,
        media_id=str(media_id),
        season_number=season_number,
        episode_number=episode_number,
    )


def build_episode_context(user, ep_item: Item) -> dict:
    season_item = ep_item.parent
    show_item = season_item.parent if season_item else None

    row = Episode.objects.filter(user=user, item=ep_item).first()
    watched = bool(row and row.status == "completed")

    rows = _build_rows(season_item, user) if season_item else []
    idx = next((i for i, r in enumerate(rows) if r.pk == ep_item.pk), None)
    prev_episode = rows[idx - 1] if idx is not None and idx > 0 else None
    next_episode = rows[idx + 1] if idx is not None and idx < len(rows) - 1 else None

    watch_rows = list(
        CompletionEvent.objects.filter(user=user, item=ep_item).order_by("-completed_at")
    )
    total_watches = len(watch_rows)
    watches = [
        {
            "completed_at": w.completed_at,
            "ordinal_label": _ordinal(total_watches - i),
        }
        for i, w in enumerate(watch_rows)
    ]

    code = (
        f"S{ep_item.season_number:02d}E{ep_item.episode_number:02d}"
        if ep_item.season_number is not None else ""
    )
    seasons = _season_summaries(show_item, user) if show_item else []

    return {
        "show": show_item,
        "season_item": season_item,
        "episode_item": ep_item,
        "episode": {
            "code": code,
            "title": ep_item.title,
            "overview": ep_item.synopsis,
            "still_url": ep_item.image,
            "air_date": ep_item.release_date,
            "runtime": ep_item.runtime,
            "watched": watched,
            "watched_at_label": _watched_at_label(row.end_date) if watched and row else "",
            "score": row.score if row else None,
            "rated_at": row.updated_at if row and row.score else None,
            "repeats": row.repeats if row else 0,
            "note": row.note if row else "",
        },
        "prev_episode": prev_episode,
        "next_episode": next_episode,
        "watches": watches,
        "season_episodes": rows,
        "seasons": seasons,
        "watched_total": sum(s.watched_count for s in seasons),
        "episode_total": sum(s.episode_count or 0 for s in seasons),
        "score_range": range(1, 11),
        "is_favorite": Favorite.objects.filter(user=user, item=ep_item).exists(),
    }


@login_required
def episode_detail(request, source: str, media_id: str, season_number: int, episode_number: int):
    ep_item = _episode_item_or_404(source, media_id, season_number, episode_number)
    ctx = build_episode_context(request.user, ep_item)
    return render(request, "tracking/episode_detail.html", ctx)


def _render_body(request, ep_item: Item) -> HttpResponse:
    ctx = build_episode_context(request.user, ep_item)
    return render(request, "tracking/partials/_episode_detail_body.html", ctx)


@login_required
@require_http_methods(["POST"])
def episode_mark_watched(request, source: str, media_id: str, season_number: int, episode_number: int):
    """Mark-watched from the episode page. Swaps `#episode-body`."""
    _load_season_item(source, media_id, season_number)  # 404s early on a bad season
    ep_item = _episode_item_or_404(source, media_id, season_number, episode_number)

    watched_on = _parse_watched_on(request)
    if isinstance(watched_on, HttpResponse):
        return watched_on

    _mark_episode_watched(request.user, ep_item, watched_on)
    return _render_body(request, ep_item)


@login_required
@require_http_methods(["POST"])
def episode_unwatch(request, source: str, media_id: str, season_number: int, episode_number: int):
    """Undo from the episode page. Swaps `#episode-body`."""
    ep_item = _episode_item_or_404(source, media_id, season_number, episode_number)
    _unwatch_episode_item(request.user, ep_item)
    return _render_body(request, ep_item)


@login_required
@require_http_methods(["POST"])
def episode_rate(request, source: str, media_id: str, season_number: int, episode_number: int):
    """Rate from the episode page. Swaps `#episode-body`."""
    try:
        score = services.parse_score(request.POST.get("score"))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    ep_item = _episode_item_or_404(source, media_id, season_number, episode_number)
    services.set_score(user=request.user, item=ep_item, score=score)
    return _render_body(request, ep_item)


@login_required
@require_http_methods(["POST"])
def episode_note(request, source: str, media_id: str, season_number: int, episode_number: int):
    """Save the private note from the episode page. Swaps `#episode-body`."""
    ep_item = _episode_item_or_404(source, media_id, season_number, episode_number)
    row, _created = Episode.objects.get_or_create(user=request.user, item=ep_item)
    row.note = request.POST.get("note", "")
    row.save()
    return _render_body(request, ep_item)
