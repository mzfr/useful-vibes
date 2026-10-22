"""Movie detail context and small HTMX write endpoints."""
from __future__ import annotations

from datetime import datetime, time

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.enums import MediaType, Status
from core.models import Favorite, Item

from . import services
from .models import CompletionEvent, Movie
from .season_views import _parse_watched_on


def _movie_or_404(source: str, media_id: str) -> Item:
    return get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.MOVIE.value,
        media_id=str(media_id),
        season_number__isnull=True,
        episode_number__isnull=True,
    )


def _genres(item: Item) -> list[str]:
    genres = (item.raw or {}).get("genres") or []
    return [g.get("name", "") for g in genres if isinstance(g, dict) and g.get("name")]


def build_movie_context(user, item: Item) -> dict:
    tracking = Movie.objects.filter(user=user, item=item).first()
    events = list(
        CompletionEvent.objects.filter(user=user, item=item).order_by("-completed_at")
    )

    raw = item.raw or {}
    studios = [
        company.get("name", "")
        for company in raw.get("production_companies", [])
        if isinstance(company, dict) and company.get("name")
    ]
    return {
        "movie": item,
        "tracking": tracking,
        "watched": bool(tracking and tracking.status == Status.COMPLETED.value),
        "watches": events,
        "watch_count": len(events),
        "genres": _genres(item),
        "studios": studios[:2],
        "score_range": range(1, 11),
        "item": item,
        "is_favorite": Favorite.objects.filter(user=user, item=item).exists(),
        "on_watchlist": bool(tracking and tracking.status == Status.PLANNING.value),
    }


def _render_body(request, item: Item) -> HttpResponse:
    return render(
        request,
        "tracking/partials/_movie_detail_body.html",
        build_movie_context(request.user, item),
    )


@login_required
@require_http_methods(["POST"])
def mark_watched(request, source: str, media_id: str):
    item = _movie_or_404(source, media_id)
    watched_on = _parse_watched_on(request)
    if isinstance(watched_on, HttpResponse):
        return watched_on
    completed_at = None
    if watched_on is not None:
        completed_at = timezone.make_aware(datetime.combine(watched_on, time(12)))
    services.record_completion(
        user=request.user,
        item=item,
        completed_at=completed_at,
        precision="date" if watched_on else "instant",
    )
    return _render_body(request, item)


@login_required
@require_http_methods(["POST"])
def unwatch(request, source: str, media_id: str):
    item = _movie_or_404(source, media_id)
    services.clear_tracking(user=request.user, item=item)
    return _render_body(request, item)


@login_required
@require_http_methods(["POST"])
def rate(request, source: str, media_id: str):
    try:
        score = services.parse_score(request.POST.get("score"))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    item = _movie_or_404(source, media_id)
    services.set_score(user=request.user, item=item, score=score)
    return _render_body(request, item)
