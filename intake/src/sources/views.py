"""
HTMX endpoints for the Sources panel: lookup + send-to-download-client.

User-visible naming is deliberately neutral ("sources", "download client") so
nothing torrent-shaped leaks into URLs, page titles, or proxy logs. The code
stays honest about what it does.
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from core import messages as user_messages
from core.enums import MediaType
from core.models import Item
from core.settings_store import qbittorrent_config

from .base import Stream, StreamProviderError, magnet_link
from .download_client import DownloadClient
from .resolve import NotResolvable, imdb_id_for
from .stremio_addon import StremioAddonBackend

_BACKEND = StremioAddonBackend()


def _item_or_404(
    source: str,
    media_type: str,
    media_id: str,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> Item:
    """Resolve the Item a sources URL points at.

    The 5-segment routes carry season/episode numbers and always resolve to
    the EPISODE row (the `media_type` in the URL is the show's, "tv", since
    stream providers key on the show's IMDb ID). Root routes must not carry
    numbers.
    """
    if season_number is not None or episode_number is not None:
        if season_number is None or episode_number is None:
            raise Http404("season and episode numbers go together")
        return get_object_or_404(
            Item,
            source=source,
            media_type=MediaType.EPISODE.value,
            media_id=str(media_id),
            season_number=season_number,
            episode_number=episode_number,
        )
    return get_object_or_404(
        Item,
        source=source,
        media_type=media_type,
        media_id=str(media_id),
        season_number__isnull=True,
        episode_number__isnull=True,
    )


def _stremio_type(media_type: str) -> str:
    if media_type in (MediaType.TV.value, MediaType.SEASON.value, MediaType.EPISODE.value):
        return "series"
    return "movie"


def _relevance_title(item: Item) -> str:
    """Title to match releases against: the show for episodes/seasons."""
    if item.media_type in (MediaType.EPISODE.value, MediaType.SEASON.value):
        show = item.parent
        while show is not None and show.media_type != MediaType.TV.value:
            show = show.parent
        if show is not None:
            return show.title
    return item.title


def _stream_context(item: Item, error: str | None = None, quality: str | None = None) -> dict:
    """Shared context builder for the panel and every error re-render."""
    ctx = {
        "item": item,
        "streams": [],
        "error": error,
        "client_ready": bool(qbittorrent_config().get("host")),
        "quality": quality or "",
        "qualities": ["2160p", "1080p", "720p"],
    }
    if error is not None:
        return ctx
    try:
        imdb_id = imdb_id_for(item)
    except NotResolvable as exc:
        ctx["error"] = str(exc)
        return ctx
    try:
        ctx["streams"] = _BACKEND.streams(
            _stremio_type(item.media_type),
            imdb_id,
            season_number=item.season_number,
            episode_number=item.episode_number,
            quality=quality or None,
            # Relevance anchor: drop releases whose name clearly isn't this
            # title (mega-packs etc). Episodes anchor on the SHOW title —
            # release names carry the show, not the episode name.
            query=_relevance_title(item),
        )
    except StreamProviderError as exc:
        ctx["error"] = str(exc)
    if not ctx["streams"] and ctx["error"] is None:
        ctx["error"] = "No sources match the current filters."
    return ctx


@login_required
def panel(
    request,
    source: str,
    media_type: str,
    media_id: str,
    season_number: int | None = None,
    episode_number: int | None = None,
):
    item = _item_or_404(source, media_type, media_id, season_number, episode_number)
    quality = request.GET.get("quality", "1080p") or None  # empty = all qualities
    ctx = _stream_context(item, quality=quality)
    ctx["panel_url"] = request.get_full_path()
    return render(request, "sources/_panel.html", ctx)


def _decode_selection(request) -> Stream | None:
    """Rebuild a minimal Stream from the POST payload; None if malformed."""
    try:
        payload = json.loads(request.POST.get("payload", ""))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    info_hash = str(payload.get("info_hash", "")).strip().lower()
    if not info_hash:
        return None
    return Stream(
        title=str(payload.get("title", "")).strip(),
        info_hash=info_hash,
    )


@login_required
@require_http_methods(["POST"])
def add(
    request,
    source: str,
    media_type: str,
    media_id: str,
    season_number: int | None = None,
    episode_number: int | None = None,
):
    """Send the selected source to the download client; re-render the panel."""
    item = _item_or_404(source, media_type, media_id, season_number, episode_number)
    stream = _decode_selection(request)
    if stream is None:
        return HttpResponseBadRequest("missing or malformed selection payload")

    quality = request.GET.get("quality", "1080p") or None
    try:
        DownloadClient().add_magnet(magnet_link(stream))
    except StreamProviderError as exc:
        user_messages.error(request.user, f"Couldn't send to download client: {exc}")
        ctx = _stream_context(item, quality=quality)
        ctx["sent_info_hash"] = None
        ctx["error"] = str(exc)
        ctx["panel_url"] = request.get_full_path()
        return render(request, "sources/_panel.html", ctx)

    user_messages.success(request.user, f"Sent to download client: {stream.title[:80]}")
    ctx = _stream_context(item, quality=quality)
    ctx["sent_info_hash"] = stream.info_hash
    ctx["panel_url"] = request.get_full_path()
    return render(request, "sources/_panel.html", ctx)
