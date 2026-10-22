"""
Shared bulk-season operations, used by both the HTMX season view and the API.

Bulk actions only fill in gaps — they mark unwatched episodes watched and
leave already-watched ones alone, so "mark whole season watched" can't
accidentally mass-rewatch episodes you've already seen. Logging an extra
watch on purpose is a per-episode action (the row's own ✓).
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item

from . import services
from .models import Episode

VALID_ACTIONS = ("all", "range")


class BulkActionError(ValueError):
    """Raised for unknown actions or missing/invalid parameters."""


def _mark_watched(user, ep_items, watched_on: _date | None = None) -> None:
    completed_at = (
        timezone.make_aware(datetime.combine(watched_on, time(12)))
        if watched_on is not None
        else timezone.now()
    )
    precision = "date" if watched_on is not None else "instant"
    for ep_item in ep_items:
        row, created = Episode.objects.get_or_create(user=user, item=ep_item)
        if not created and row.status == Status.COMPLETED.value:
            continue  # already watched — bulk actions don't force a rewatch
        services.record_completion(
            user=user,
            item=ep_item,
            completed_at=completed_at,
            precision=precision,
        )


@transaction.atomic
def apply_bulk_action(
    *,
    user,
    season_item: Item,
    action: str,
    start: int | None = None,
    end: int | None = None,
    watched_on: _date | None = None,
) -> None:
    """Apply a bulk action to every episode under `season_item`.

    Actions:
      - "all"    mark every episode watched (again, for any already watched)
      - "range"  mark episodes start..end inclusive (auto-swaps bounds)

    `watched_on` backdates the completion; defaults to today.
    """
    ep_qs = Item.objects.filter(
        parent=season_item, media_type=MediaType.EPISODE.value
    ).order_by("episode_number")

    if action == "all":
        _mark_watched(user, list(ep_qs), watched_on)
    elif action == "range":
        if start is None or end is None:
            raise BulkActionError("range needs integer start/end")
        lo, hi = sorted((start, end))
        _mark_watched(
            user,
            list(ep_qs.filter(episode_number__gte=lo, episode_number__lte=hi)),
            watched_on,
        )
    else:
        raise BulkActionError(f"unknown action: {action!r}")
