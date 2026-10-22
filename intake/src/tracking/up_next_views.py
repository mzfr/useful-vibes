"""HTMX endpoints for the Up Next feed (mark-watched on the next episode)."""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.enums import MediaType
from core.models import Item
from providers.base import NotSupported, ProviderError

from . import services
from .models import ShowAutomaticDownload
from .season_views import (
    _build_rows,
    _load_or_fetch_season,
    _mark_episode_watched,
    _next_episode,
)
from .up_next import in_progress_movies, shows_for_user


def _render_feed(request, message: str):
    resp = render(
        request,
        "tracking/_up_next.html",
        {
            "show_rows": shows_for_user(request.user),
            "movie_rows": in_progress_movies(request.user),
        },
    )
    resp["HX-Trigger"] = json.dumps({"toast": {"msg": message}})
    return resp


def _root_show(item: Item) -> Item | None:
    """Resolve a show, season, or episode to its root TV show."""
    if item.media_type == MediaType.TV.value:
        return item
    if item.media_type == MediaType.SEASON.value:
        return item.parent
    if item.media_type == MediaType.EPISODE.value and item.parent:
        return item.parent.parent
    return None


@login_required
@require_http_methods(["POST"])
def discontinue_item(request, item_id: int):
    """Hide an item's whole show from Up Next without changing watch history."""
    try:
        item = Item.objects.select_related("parent__parent").get(pk=item_id)
    except Item.DoesNotExist:
        return HttpResponseBadRequest("no such item")

    show = _root_show(item)
    if show is None or show.media_type != MediaType.TV.value:
        return HttpResponseBadRequest("item does not belong to a TV show")

    services.set_show_visibility(user=request.user, show=show, up_next=True)
    return _render_feed(request, f"{show.title} discontinued")


@login_required
@require_http_methods(["POST"])
def mark_next_watched(request, episode_item_id: int):
    """Mark the given episode watched, then re-render the matching Up Next row.

    Same rule as everywhere else: if it's already watched, this logs another
    watch instead of no-op'ing — Up Next only ever offers the next *unwatched*
    episode though, so that branch shouldn't normally trigger here.
    """
    try:
        ep_item = Item.objects.get(pk=episode_item_id, media_type=MediaType.EPISODE.value)
    except Item.DoesNotExist:
        return HttpResponseBadRequest("no such episode")

    _mark_episode_watched(request.user, ep_item, watched_on=None)

    code = ""
    if ep_item.season_number is not None and ep_item.episode_number is not None:
        code = f"S{ep_item.season_number:02d}E{ep_item.episode_number:02d}"

    # Re-render the whole Up Next feed so the next episode slides in.
    return _render_feed(request, f"{code} marked watched".strip())


@login_required
@require_http_methods(["POST"])
def toggle_automatic_download(
    request,
    show_item_id: int,
    start_season_number: int,
    start_episode_number: int,
):
    """Enable from the current Up Next episode, or disable for this show."""
    try:
        show = Item.objects.get(pk=show_item_id, media_type=MediaType.TV.value)
    except Item.DoesNotExist:
        return HttpResponseBadRequest("no such show")

    existing = ShowAutomaticDownload.objects.filter(user=request.user, show=show).first()
    if existing is not None:
        existing.delete()
        return _render_feed(request, f"Automatic downloads disabled for {show.title}")

    from sources.automatic import first_unwatched_episode, process_subscription

    episode = first_unwatched_episode(request.user, show)
    if episode is not None:
        start_season_number = episode.season_number
        start_episode_number = episode.episode_number
    else:
        today = timezone.localdate().isoformat()
        valid_new_season = start_episode_number == 1 and any(
            raw.get("season_number") == start_season_number
            and raw.get("air_date")
            and raw["air_date"] <= today
            for raw in (show.raw or {}).get("seasons", [])
        )
        if not valid_new_season:
            return HttpResponseBadRequest("show has no next unwatched episode")

    subscription = ShowAutomaticDownload.objects.create(
        user=request.user,
        show=show,
        start_season_number=start_season_number,
        start_episode_number=start_episode_number,
    )
    result = process_subscription(subscription)
    if result.sent:
        count = f"{result.sent} episode{'s' if result.sent != 1 else ''}"
        message = f"Automatic downloads enabled for {show.title}; sent {count}"
    elif result.errors:
        message = f"Automatic downloads enabled for {show.title}; unavailable episodes will retry"
    else:
        message = f"Automatic downloads enabled for {show.title}"
    return _render_feed(request, message)


@login_required
@require_http_methods(["POST"])
def mark_new_season_next_watched(
    request, source: str, media_id: str, season_number: int,
):
    """Materialize a discovered season and mark its first aired unseen episode."""
    try:
        season_item = _load_or_fetch_season(source, media_id, season_number)
    except (NotSupported, ProviderError) as exc:
        return HttpResponseBadRequest(str(exc))

    episode = _next_episode(_build_rows(season_item, request.user))
    if episode is None:
        return HttpResponseBadRequest("no aired unwatched episode in this season")
    ep_item = Item.objects.get(pk=episode.pk)
    _mark_episode_watched(request.user, ep_item, watched_on=None)

    return _render_feed(request, f"{episode.code} marked watched")


@login_required
@require_http_methods(["POST"])
def mark_item_finished(request, item_id: int):
    """Finish an in-progress movie/book directly from its Up Next card."""
    try:
        item = Item.objects.get(
            pk=item_id, media_type__in=(MediaType.MOVIE.value, MediaType.BOOK.value),
        )
    except Item.DoesNotExist:
        return HttpResponseBadRequest("no such item")
    services.record_completion(user=request.user, item=item)
    verb = "finished" if item.media_type == MediaType.BOOK.value else "marked watched"
    return _render_feed(request, f"{item.title} {verb}")
