"""
Service layer between providers and tracking models.

Anything that needs an Item from a Metadata payload (search → track flow,
importers, API endpoints) goes through `upsert_item`. That gives us one place
to keep the parent/child wiring and the natural-key dedup honest.
"""
from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item

from .models import MODEL_FOR_MEDIA_TYPE, Media


def _payload_to_item_fields(payload: Any) -> dict[str, Any]:
    """Translate a Metadata-shaped object into Item field values."""
    return {
        "title": payload.title,
        "image": payload.image or "",
        "synopsis": payload.synopsis or "",
        "release_date": payload.release_date,
        "runtime": payload.runtime,
        "max_progress": payload.max_progress,
        "raw": payload.raw or {},
    }


@transaction.atomic
def upsert_item(
    payload: Any,
    *,
    season_number: int | None = None,
    episode_number: int | None = None,
    parent: Item | None = None,
) -> Item:
    """Create or refresh an Item from a provider Metadata payload.

    `season_number` and `episode_number` are inferred from the payload for
    SeasonMetadata / EpisodeMetadata but can be overridden by the caller.
    """
    media_type = payload.media_type
    s_num = season_number if season_number is not None else getattr(payload, "season_number", None)
    e_num = episode_number if episode_number is not None else getattr(payload, "episode_number", None)

    # Honour the schema: only seasons/episodes carry numbers.
    if media_type not in (MediaType.SEASON.value, MediaType.EPISODE.value):
        s_num = None
        e_num = None
    elif media_type == MediaType.SEASON.value:
        e_num = None

    natural_key = {
        "source": payload.source,
        "media_type": media_type,
        "media_id": str(payload.media_id),
        "season_number": s_num,
        "episode_number": e_num,
    }
    defaults = _payload_to_item_fields(payload)
    defaults["metadata_complete"] = True
    defaults["metadata_fetched_at"] = timezone.now()
    if parent is not None:
        defaults["parent"] = parent

    item, _created = Item.objects.update_or_create(defaults=defaults, **natural_key)
    return item


@transaction.atomic
def upsert_season_with_episodes(season_payload: Any, *, tv_item: Item) -> Item:
    """Idempotently create a season Item + every episode Item under it.

    Also removes orphan stub episodes left behind by earlier test/stub runs:
    any episode under this season that has no real metadata and no user
    tracking rows is deleted so the catalog stays clean.
    """
    season_item = upsert_item(
        season_payload,
        season_number=season_payload.season_number,
        parent=tv_item,
    )
    real_ep_numbers = set()
    for ep in season_payload.episodes:
        upsert_item(
            ep,
            season_number=season_payload.season_number,
            episode_number=ep.episode_number,
            parent=season_item,
        )
        real_ep_numbers.add(ep.episode_number)

    # Remove orphan stubs: episodes with no real metadata and no tracking.
    from tracking.models import Episode as TrackingEpisode
    orphans = Item.objects.filter(
        parent=season_item,
        media_type=MediaType.EPISODE.value,
        metadata_complete=False,
    ).exclude(pk__in=TrackingEpisode.objects.values_list("item_id", flat=True))
    orphans.delete()

    return season_item


def resolve_root_item(source: str, media_type: str, media_id: str):
    """Load complete root metadata locally, fetching it only when needed."""
    from providers import registry

    item = Item.objects.filter(
        source=source,
        media_type=media_type,
        media_id=str(media_id),
        season_number__isnull=True,
        episode_number__isnull=True,
    ).first()
    if item is not None and item.metadata_complete:
        return item, None
    metadata = registry.get(source).details(media_type, media_id)
    return upsert_item(metadata), metadata


def materialize_season(provider, media_id: str, season_number: int) -> Item:
    """Fetch and persist a show, one season, and that season's episodes."""
    show = upsert_item(provider.details(MediaType.TV.value, media_id))
    season = provider.season(media_id, season_number)
    return upsert_season_with_episodes(season, tv_item=show)


def get_or_create_tracking(
    *,
    user,
    item: Item,
    defaults: dict[str, Any] | None = None,
) -> tuple[Media, bool]:
    """Return (tracking_row, created) for the user/item pair."""
    model = MODEL_FOR_MEDIA_TYPE[item.media_type]
    row, created = model.objects.get_or_create(
        user=user, item=item, defaults=defaults or {}
    )
    return row, created


@transaction.atomic
def update_tracking(
    *,
    user,
    item: Item,
    score: int | None = None,
    progress: int | None = None,
    status: str | None = None,
    start_date=None,
    end_date=None,
    bump_progressed_at: bool = True,
) -> Media:
    """Idempotent upsert for a tracking row. Only mutates fields the caller passed."""
    row, _created = get_or_create_tracking(user=user, item=item)
    if score is not None:
        row.score = score
    if progress is not None:
        row.progress = progress
    if status is not None:
        # Clear conflicting completion state before process_status runs.
        if status != Status.COMPLETED.value and row.status == Status.COMPLETED.value:
            row.end_date = None
            if row.item.max_progress is not None and row.progress >= row.item.max_progress:
                row.progress = 0
        row.status = status
    if start_date is not None:
        row.start_date = start_date
    if end_date is not None:
        row.end_date = end_date
    if bump_progressed_at:
        row.progressed_at = timezone.now()
    row.save()
    return row


def parse_score(raw: str | None) -> int:
    try:
        score = int(raw or "0")
    except ValueError as exc:
        raise ValueError("score must be an integer") from exc
    if not 0 <= score <= 10:
        raise ValueError("score must be 0..10")
    return score


def set_score(*, user, item: Item, score: int) -> Media:
    row, _created = get_or_create_tracking(user=user, item=item)
    row.score = score or None
    row.save()
    return row


def clear_tracking(*, user, item: Item) -> None:
    model = MODEL_FOR_MEDIA_TYPE[item.media_type]
    row = model.objects.filter(user=user, item=item).first()
    if row is None:
        return
    row.status = Status.PLANNING.value
    row.progress = 0
    row.end_date = None
    row.progressed_at = timezone.now()
    row.save()


def set_show_visibility(
    *, user, show: Item, up_next: bool | None = None, releases: bool | None = None,
    hidden_at=None,
):
    """Set independent feed visibility flags without touching tracking rows."""
    from releases.models import ReleaseCheck

    from .models import ShowFeedPreference

    if show.media_type != MediaType.TV.value:
        raise ValueError("feed visibility is only available for TV shows")
    pref, _ = ShowFeedPreference.objects.get_or_create(user=user, show=show)
    stamp = hidden_at or timezone.now()
    if up_next is not None:
        pref.up_next_hidden_at = stamp if up_next else None
    if releases is not None:
        pref.releases_hidden_at = stamp if releases else None
        if releases:
            ReleaseCheck.objects.filter(user=user, item=show).update(seen_at=timezone.now())
    if pref.up_next_hidden_at is None and pref.releases_hidden_at is None:
        pref.delete()
        return None
    pref.save()
    return pref


@transaction.atomic
def record_completion(
    *, user, item: Item, completed_at=None, source="manual", source_event_id=None,
    precision="instant",
):
    """Create one idempotent completion and synchronize current tracking state."""
    from .models import CompletionEvent

    completed_at = completed_at or timezone.now()
    event, created = CompletionEvent.objects.get_or_create(
        user=user,
        source=source,
        source_event_id=source_event_id,
        defaults={
            "item": item,
            "completed_at": completed_at,
            "precision": precision,
        },
    ) if source_event_id is not None else (
        CompletionEvent.objects.create(
            user=user, item=item, completed_at=completed_at,
            source=source, precision=precision,
        ), True
    )
    if not created and (
        event.item_id != item.pk
        or event.completed_at != completed_at
        or event.precision != precision
    ):
        raise ValueError(f"source event {source}:{source_event_id} conflicts with existing data")

    row, _ = get_or_create_tracking(user=user, item=item)
    count = CompletionEvent.objects.filter(user=user, item=item).count()
    row.repeats = max(count - 1, 0)
    row.status = Status.COMPLETED.value
    row.progress = item.max_progress or max(row.progress, 1)
    row.end_date = timezone.localtime(completed_at).date()
    row.progressed_at = completed_at
    row.save()
    if item.media_type == MediaType.EPISODE.value:
        promote_episode_parents(user=user, episode=item)
    return row, event, created


def promote_episode_parents(*, user, episode: Item) -> None:
    """A watched episode means a planning season/show has been started."""
    for parent in (episode.parent, episode.parent.parent if episode.parent else None):
        if parent is None:
            continue
        model = MODEL_FOR_MEDIA_TYPE.get(parent.media_type)
        if model is None:
            continue
        planning = model.objects.filter(
            user=user, item=parent, status=Status.PLANNING.value
        ).first()
        if planning is not None:
            planning.status = Status.IN_PROGRESS.value
            planning.progressed_at = timezone.now()
            planning.save()


def mark_watched(*, user, item: Item) -> Media:
    """Record a completed watch/read and return current tracking state."""
    row, _event, _created = record_completion(user=user, item=item)
    return row
