"""
Season-detail view + single-row and bulk endpoints.

Single-episode actions (toggle/rate) swap just that row (`#episode-{pk}`,
`partials/_episode_row.html`). Bulk actions swap that season's whole body
(`#season-panel-body-{n}`, `partials/_season_panel.html`) since they can
touch many rows at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, time

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.enums import MediaType, Status
from core.models import Item
from providers import registry
from providers.base import NotSupported, ProviderError

from . import services
from .models import Episode


@dataclass
class _EpisodeRow:
    """A flattened episode row used by _episode_row.html / _season_panel.html."""

    pk: int
    season_number: int
    episode_number: int
    title: str
    air_date: _date | None
    aired: bool
    watched: bool
    watched_at_label: str
    score: int | None
    repeats: int = 0

    @property
    def code(self) -> str:
        return f"S{self.season_number:02d}E{self.episode_number:02d}"


def _watched_at_label(end_date: _date | None) -> str:
    if end_date is None:
        return ""
    today = timezone.localdate()
    if end_date == today:
        return "today"
    if end_date == today - _date.resolution:
        return "yesterday"
    return end_date.strftime("%-d %b")


def _build_rows_for_seasons(season_items: list[Item], user) -> dict[int, list[_EpisodeRow]]:
    """Build episode rows for several seasons with two queries total."""
    episode_items = list(
        Item.objects.filter(parent__in=season_items, media_type=MediaType.EPISODE.value).order_by(
            "season_number", "episode_number"
        )
    )
    tracking_by_item = {
        e.item_id: e for e in Episode.objects.filter(user=user, item__in=episode_items)
    }
    today = timezone.localdate()
    grouped: dict[int, list[_EpisodeRow]] = {season.pk: [] for season in season_items}
    for it in episode_items:
        ep = tracking_by_item.get(it.pk)
        watched = bool(ep and ep.status == Status.COMPLETED.value)
        grouped[it.parent_id].append(
            _EpisodeRow(
                pk=it.pk,
                season_number=it.season_number or 0,
                episode_number=it.episode_number or 0,
                title=it.title,
                air_date=it.release_date,
                # Unknown air date doesn't block "next up" — same convention
                # as the Up Next feed (tracking.up_next.shows_for_user).
                aired=it.release_date is None or it.release_date <= today,
                watched=watched,
                watched_at_label=_watched_at_label(ep.end_date) if watched and ep else "",
                score=ep.score if ep else None,
                repeats=ep.repeats if ep else 0,
            )
        )
    return grouped


def _build_rows(season_item: Item, user) -> list[_EpisodeRow]:
    """Combine one season's episode Items with the user's tracking rows."""
    return _build_rows_for_seasons([season_item], user).get(season_item.pk, [])


def _next_episode(rows: list[_EpisodeRow]) -> _EpisodeRow | None:
    return next((r for r in rows if r.aired and not r.watched), None)


def _load_or_fetch_season(source: str, media_id: str, season_number: int) -> Item:
    """Load a season locally, materializing its episodes once when needed."""
    season_item = (
        Item.objects.filter(
            source=source,
            media_type=MediaType.SEASON.value,
            media_id=str(media_id),
            season_number=season_number,
        )
        .select_related("parent")
        .first()
    )

    if season_item is None:
        provider = registry.get(source)
        season_item = services.materialize_season(provider, media_id, season_number)

    return season_item


@login_required
def season_detail(request, source: str, media_id: str, season_number: int):
    """Keep old season links working by opening that season on the show page."""
    url = reverse("tracking:details", args=(media_id,))
    return redirect(url)


@login_required
def season_panel(request, source: str, media_id: str, season_number: int):
    """Return one season's episode rows for the inline accordion."""
    try:
        season_item = _load_or_fetch_season(source, media_id, season_number)
    except (NotSupported, ProviderError) as exc:
        return render(request, "tracking/details_error.html", {"error": str(exc)}, status=502)
    return render(
        request,
        "tracking/partials/_season_panel.html",
        {
            "show": season_item.parent,
            "season_number": season_number,
            "episodes": _build_rows(season_item, request.user),
            "score_range": range(1, 11),
        },
    )


@login_required
def new_season_next_episode(request, source: str, media_id: str, season_number: int):
    """Materialize a new season, then open its first aired unwatched episode."""
    try:
        season_item = _load_or_fetch_season(source, media_id, season_number)
    except (NotSupported, ProviderError) as exc:
        return render(request, "tracking/details_error.html", {"error": str(exc)}, status=502)

    next_episode = _next_episode(_build_rows(season_item, request.user))
    if next_episode is None:
        return redirect("tracking:details", media_id)
    return redirect(
        "tracking:episode_detail",
        media_id,
        season_number,
        next_episode.episode_number,
    )


# ---------------------------------------------------------------------- writes


def _episode_item_or_400(season_item: Item, episode_number: int) -> Item | HttpResponse:
    it = Item.objects.filter(
        parent=season_item,
        media_type=MediaType.EPISODE.value,
        episode_number=episode_number,
    ).first()
    if not it:
        return HttpResponseBadRequest(f"no episode {episode_number} under season {season_item.pk}")
    return it


def _render_row(request, ep_item: Item, score_range=None) -> HttpResponse:
    season_item = ep_item.parent
    score_range = score_range or range(1, 11)
    rows = _build_rows(season_item, request.user)
    row = next(r for r in rows if r.pk == ep_item.pk)
    return render(
        request,
        "tracking/partials/_episode_row.html",
        {"e": row, "show": season_item.parent, "score_range": score_range},
    )


def _render_list(request, season_item: Item) -> HttpResponse:
    """Re-render one season's rows after a bulk action touches many of them."""
    return render(
        request,
        "tracking/partials/_season_panel.html",
        {
            "episodes": _build_rows(season_item, request.user),
            "show": season_item.parent,
            "season_number": season_item.season_number,
            "score_range": range(1, 11),
        },
    )


def _load_season_item(source: str, media_id: str, season_number: int) -> Item:
    # 404 (not 500) when the season was never materialized — stale tab,
    # hand-typed URL, etc.
    return get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.SEASON.value,
        media_id=str(media_id),
        season_number=season_number,
    )


def _parse_watched_on(request) -> _date | None | HttpResponse:
    """Optional `watched_on=YYYY-MM-DD` from a POST body. None means "today"."""
    raw = (request.POST.get("watched_on") or "").strip()
    if not raw:
        return None
    try:
        return _date.fromisoformat(raw)
    except ValueError:
        return HttpResponseBadRequest("watched_on must be YYYY-MM-DD")


@transaction.atomic
def _mark_episode_watched(user, ep_item: Item, watched_on: _date | None) -> None:
    """One click, one meaning: this episode was watched (again, if it
    already was). Never silently un-marks — see `_unwatch_episode_item` for
    the separate undo action."""
    row, created = Episode.objects.get_or_create(user=user, item=ep_item)
    if not created and row.status == Status.COMPLETED.value:
        row.mark_watched_again(watched_on=watched_on)
        return
    completed_at = None
    precision = "instant"
    if watched_on is not None:
        completed_at = timezone.make_aware(datetime.combine(watched_on, time(12)))
        precision = "date"
    services.record_completion(
        user=user,
        item=ep_item,
        completed_at=completed_at,
        precision=precision,
    )


def _unwatch_episode_item(user, ep_item: Item) -> None:
    """Undo — clears watched state entirely. A separate, deliberate action
    from marking watched, not the same button toggled twice."""
    services.clear_tracking(user=user, item=ep_item)


@login_required
@require_http_methods(["POST"])
def toggle_episode(request, source: str, media_id: str, season_number: int, episode_number: int):
    """Mark an episode watched. Swaps just that row.

    If the episode is already watched, this logs another watch (bumps its
    rewatch count) instead of un-marking it — clicking "watched" twice means
    "I watched it again," not "undo." Accepts an optional `watched_on` date
    so a past viewing can be logged on the day it actually happened.
    """
    season_item = _load_season_item(source, media_id, season_number)
    ep_item = _episode_item_or_400(season_item, episode_number)
    if isinstance(ep_item, HttpResponse):
        return ep_item

    watched_on = _parse_watched_on(request)
    if isinstance(watched_on, HttpResponse):
        return watched_on

    _mark_episode_watched(request.user, ep_item, watched_on)
    return _render_row(request, ep_item)


@login_required
@require_http_methods(["POST"])
def unwatch_episode(request, source: str, media_id: str, season_number: int, episode_number: int):
    """Undo — clear watched state on a single episode. Swaps just that row."""
    season_item = _load_season_item(source, media_id, season_number)
    ep_item = _episode_item_or_400(season_item, episode_number)
    if isinstance(ep_item, HttpResponse):
        return ep_item

    _unwatch_episode_item(request.user, ep_item)
    return _render_row(request, ep_item)


@login_required
@require_http_methods(["POST"])
def rate_episode(request, source: str, media_id: str, season_number: int, episode_number: int):
    """Set the score on a single episode (1–10, or 0 to clear). Swaps just that row."""
    try:
        score = services.parse_score(request.POST.get("score"))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    season_item = _load_season_item(source, media_id, season_number)
    ep_item = _episode_item_or_400(season_item, episode_number)
    if isinstance(ep_item, HttpResponse):
        return ep_item

    services.set_score(user=request.user, item=ep_item, score=score)
    return _render_row(request, ep_item)


@login_required
@require_http_methods(["POST"])
def bulk_season(request, source: str, media_id: str, season_number: int):
    """Bulk ops on a season. Delegates to tracking.bulk (shared with the API).
    Swaps the whole episode list. Marking an already-watched episode logs
    another watch rather than skipping it — same rule as the single toggle.

    POST `action` is one of:
      - "all"    → mark every episode watched
      - "range"  → POST `start`, `end` (inclusive ep numbers)

    POST `watched_on` (YYYY-MM-DD, optional) backdates the completion.
    """
    from .bulk import BulkActionError, apply_bulk_action

    season_item = _load_season_item(source, media_id, season_number)
    action = request.POST.get("action", "")

    watched_on = _parse_watched_on(request)
    if isinstance(watched_on, HttpResponse):
        return watched_on

    start = end = None
    if action == "range":
        try:
            start = int(request.POST.get("start", ""))
            end = int(request.POST.get("end", ""))
        except ValueError:
            return HttpResponseBadRequest("range needs integer start/end")

    try:
        apply_bulk_action(
            user=request.user,
            season_item=season_item,
            action=action,
            start=start,
            end=end,
            watched_on=watched_on,
        )
    except BulkActionError as exc:
        return HttpResponseBadRequest(str(exc))

    return _render_list(request, season_item)
