"""Automatic source selection and episode delivery for opted-in shows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from providers import registry
from providers.base import ProviderError
from tracking import services
from tracking.models import (
    AutomaticEpisodeDownload,
    Episode,
    ShowAutomaticDownload,
)

from .base import Stream, StreamProviderError, magnet_link
from .download_client import DownloadClient
from .resolve import NotResolvable, imdb_id_for
from .stremio_addon import StremioAddonBackend

MIN_EPISODE_SIZE_MB = 1024
MAX_EPISODE_SIZE_MB = 10 * 1024
_RETRY_COOLDOWN = timedelta(minutes=10)
_LOW_QUALITIES = {"480p", "360p", "240p"}
_BACKEND = StremioAddonBackend()


@dataclass
class ProcessResult:
    checked: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def choose_stream(streams: list[Stream]) -> Stream | None:
    """Pick the most seeded usable source for one episode.

    Require a known size between 1 and 10 GiB and reject sub-720p sources.
    """
    eligible = [
        stream
        for stream in streams
        if stream.size_mb is not None
        and MIN_EPISODE_SIZE_MB <= stream.size_mb <= MAX_EPISODE_SIZE_MB
        and stream.quality.lower() not in _LOW_QUALITIES
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda stream: (
            stream.seeders if stream.seeders is not None else -1,
            -(stream.size_mb if stream.size_mb is not None else MAX_EPISODE_SIZE_MB),
        ),
    )


def first_unwatched_episode(user, show: Item) -> Item | None:
    """Return the Up Next cursor used when automatic downloads are enabled."""
    watched_ids = Episode.objects.filter(
        user=user, status=Status.COMPLETED.value
    ).values_list("item_id", flat=True)
    today = timezone.localdate()
    return (
        Item.objects.filter(
            media_type=MediaType.EPISODE.value,
            parent__parent=show,
        )
        .filter(Q(release_date__isnull=True) | Q(release_date__lte=today))
        .exclude(pk__in=watched_ids)
        .order_by("season_number", "episode_number")
        .first()
    )


def _raw_air_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _refresh_catalog(subscription: ShowAutomaticDownload, result: ProcessResult) -> None:
    """Refresh TMDB show metadata and seasons that can contain new aired episodes."""
    show = subscription.show
    try:
        provider = registry.get(show.source)
        # Scheduled commands are fresh processes, but the web request may have
        # a cached provider. Automatic checks need current air dates.
        fresh_provider = getattr(provider, "inner", provider)
        metadata = fresh_provider.details(MediaType.TV.value, show.media_id)
        show = services.upsert_item(metadata)
    except (ProviderError, ValueError) as exc:
        result.errors.append(f"{show.title}: metadata refresh failed: {exc}")
        return

    today = timezone.localdate()
    season_rows = []
    for raw in (show.raw or {}).get("seasons", []):
        number = raw.get("season_number")
        air_date = _raw_air_date(raw.get("air_date"))
        if (
            isinstance(number, int)
            and number >= subscription.start_season_number
            and number > 0
            and air_date is not None
            and air_date <= today
        ):
            season_rows.append((number, int(raw.get("episode_count") or 0)))
    if not season_rows:
        return

    latest_number = max(number for number, _count in season_rows)
    for number, expected_count in season_rows:
        local = Item.objects.filter(
            source=show.source,
            media_type=MediaType.SEASON.value,
            media_id=show.media_id,
            season_number=number,
        ).first()
        local_episodes = (
            Item.objects.filter(parent=local, media_type=MediaType.EPISODE.value)
            if local is not None
            else Item.objects.none()
        )
        needs_refresh = (
            local is None
            or number == latest_number
            or local_episodes.count() < expected_count
            or local_episodes.filter(release_date__isnull=True).exists()
        )
        if not needs_refresh:
            continue
        try:
            payload = fresh_provider.season(show.media_id, number)
            services.upsert_season_with_episodes(payload, tv_item=show)
        except (ProviderError, ValueError) as exc:
            result.errors.append(f"{show.title} season {number}: refresh failed: {exc}")


def _candidate_episodes(subscription: ShowAutomaticDownload) -> list[Item]:
    watched_ids = Episode.objects.filter(
        user=subscription.user, status=Status.COMPLETED.value
    ).values_list("item_id", flat=True)
    cursor = Q(season_number__gt=subscription.start_season_number) | Q(
        season_number=subscription.start_season_number,
        episode_number__gte=subscription.start_episode_number,
    )
    return list(
        Item.objects.filter(
            cursor,
            media_type=MediaType.EPISODE.value,
            parent__parent=subscription.show,
            release_date__lte=timezone.localdate(),
        )
        .exclude(pk__in=watched_ids)
        .order_by("season_number", "episode_number")
    )


def _claim(user, episode: Item) -> AutomaticEpisodeDownload | None:
    delivery, _created = AutomaticEpisodeDownload.objects.get_or_create(
        user=user, item=episode
    )
    now = timezone.now()
    claimed = (
        AutomaticEpisodeDownload.objects.filter(
            pk=delivery.pk,
            status=AutomaticEpisodeDownload.STATUS_PENDING,
        )
        .filter(Q(attempted_at__isnull=True) | Q(attempted_at__lte=now - _RETRY_COOLDOWN))
        .update(attempted_at=now, last_error="")
    )
    if not claimed:
        return None
    delivery.attempted_at = now
    delivery.last_error = ""
    return delivery


def _record_error(
    delivery: AutomaticEpisodeDownload, message: str, result: ProcessResult
) -> None:
    delivery.last_error = message
    delivery.save(update_fields=["last_error"])
    result.failed += 1
    result.errors.append(message)


def process_subscription(
    subscription: ShowAutomaticDownload, *, refresh: bool = True
) -> ProcessResult:
    """Submit every aired, unwatched episode at or after the saved cursor."""
    result = ProcessResult()
    if refresh:
        _refresh_catalog(subscription, result)
        subscription.refresh_from_db()

    episodes = _candidate_episodes(subscription)
    if not episodes:
        return result

    try:
        imdb_id = imdb_id_for(subscription.show)
    except NotResolvable as exc:
        result.failed += len(episodes)
        result.errors.append(str(exc))
        return result

    client = DownloadClient()
    for episode in episodes:
        delivery = _claim(subscription.user, episode)
        if delivery is None:
            result.skipped += 1
            continue
        result.checked += 1
        code = f"S{episode.season_number:02d}E{episode.episode_number:02d}"
        try:
            streams = _BACKEND.streams(
                "series",
                imdb_id,
                season_number=episode.season_number,
                episode_number=episode.episode_number,
                query=subscription.show.title,
            )
        except StreamProviderError as exc:
            _record_error(delivery, f"{subscription.show.title} {code}: {exc}", result)
            continue

        selected = choose_stream(streams)
        if selected is None:
            _record_error(
                delivery,
                f"{subscription.show.title} {code}: no source matched the automatic rules",
                result,
            )
            continue

        try:
            client.add_magnet(
                magnet_link(selected),
                show_title=subscription.show.title,
                season_number=episode.season_number,
            )
        except StreamProviderError as exc:
            _record_error(
                delivery,
                f"{subscription.show.title} {code}: download client rejected the source: {exc}",
                result,
            )
            continue

        delivery.status = AutomaticEpisodeDownload.STATUS_SENT
        delivery.source_title = selected.title
        delivery.info_hash = selected.info_hash
        delivery.sent_at = timezone.now()
        delivery.last_error = ""
        delivery.save(
            update_fields=["status", "source_title", "info_hash", "sent_at", "last_error"]
        )
        result.sent += 1
    return result


def process_all_subscriptions() -> ProcessResult:
    """Process every enabled show; intended for the scheduled command."""
    total = ProcessResult()
    subscriptions = ShowAutomaticDownload.objects.select_related("user", "show").all()
    for subscription in subscriptions:
        result = process_subscription(subscription)
        total.checked += result.checked
        total.sent += result.sent
        total.skipped += result.skipped
        total.failed += result.failed
        total.errors.extend(result.errors)
    return total
