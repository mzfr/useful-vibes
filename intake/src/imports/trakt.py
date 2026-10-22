"""
Trakt dataset → tracking rows.

Trakt restricted API access to VIP accounts, so there is no OAuth flow here.
Datasets come from the official data export (see `trakt_export.py`), which any
account can download from https://trakt.tv/settings/data.

Items resolve to TMDB via each entry's `ids.tmdb` field, so TMDB_API_KEY is
still required. Everything runs inline in the request thread for the
single-user model — no Celery. The ImportJob row backs an HTMX progress poll.
"""
from __future__ import annotations

from datetime import UTC, datetime

from django.utils import timezone

from core.enums import MediaType, Status
from providers.base import ProviderError
from tracking import services


def _ts(raw: str | None):
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def process_datasets(
    user, job, *, history, ratings, watchlist, list_items, needs_review=None,
):
    """Translate Trakt export datasets into tracking rows."""
    needs_review = needs_review if needs_review is not None else []

    from .catalog import build_catalog_plan, enrich_catalog
    plan = build_catalog_plan(history, ratings, watchlist, list_items)
    job.phase = "catalog"
    job.save(update_fields=["phase"])
    needs_review.extend(enrich_catalog(plan))
    job.phase = "tracking"

    job.total = (
        len(history)
        + len(ratings)
        + len(watchlist)
        + sum(len(items) for _, items in list_items)
    )
    job.save(update_fields=["total"])

    # Index ratings by (type, tmdb_id, season, episode) for fast lookup.
    rating_lookup: dict[tuple, int] = {}
    for r in ratings:
        kind = r.get("type")
        rating = r.get("rating")
        if kind == "movie":
            tmdb_id = ((r.get("movie") or {}).get("ids") or {}).get("tmdb")
            if tmdb_id:
                rating_lookup[("movie", str(tmdb_id), None, None)] = rating
        elif kind == "show":
            tmdb_id = ((r.get("show") or {}).get("ids") or {}).get("tmdb")
            if tmdb_id:
                rating_lookup[("show", str(tmdb_id), None, None)] = rating
        elif kind == "episode":
            show_ids = ((r.get("show") or {}).get("ids") or {})
            tmdb_id = show_ids.get("tmdb")
            ep = r.get("episode") or {}
            if tmdb_id and ep.get("season") is not None and ep.get("number") is not None:
                rating_lookup[("episode", str(tmdb_id), ep["season"], ep["number"])] = rating

    # Group history by natural key so multiple plays of the same thing become
    # ONE tracking row with repeats = plays - 1. Rewatch counts are a headline
    # feature; collapsing them to a single watch would lose data.
    # grouped[key] = {"entry": <representative>, "plays": N, "latest": datetime|None}
    grouped: dict[tuple, dict] = {}
    for entry in history:
        kind = entry.get("type")
        watched_at = _ts(entry.get("watched_at"))
        if kind == "movie":
            tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
            key = ("movie", str(tmdb_id), None, None) if tmdb_id else None
        elif kind == "episode":
            tmdb_id = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
            ep = entry.get("episode") or {}
            sn, en = ep.get("season"), ep.get("number")
            key = (
                ("episode", str(tmdb_id), sn, en)
                if tmdb_id and sn is not None and en is not None
                else None
            )
        else:
            key = None

        if key is None:
            needs_review.append({"reason": "unresolvable history entry", "entry": entry})
            job.processed += 1
            continue

        bucket = grouped.setdefault(key, {"entry": entry, "entries": [], "dates": []})
        bucket["entries"].append(entry)
        bucket["dates"].append(watched_at)

    for key, bucket in grouped.items():
        kind, tmdb_id, sn, en = key
        plays = len(bucket["dates"])
        try:
            if kind == "movie":
                from core.models import Item
                item = Item.objects.filter(
                    source="tmdb", media_type=MediaType.MOVIE.value, media_id=tmdb_id
                ).first()
                if item:
                    for entry, watched_at in sorted(
                        zip(bucket["entries"], bucket["dates"], strict=True),
                        key=lambda pair: pair[1] or datetime.min.replace(tzinfo=UTC),
                    ):
                        if watched_at:
                            services.record_completion(
                                user=user, item=item,
                                completed_at=watched_at,
                                source="trakt", source_event_id=str(entry["id"]),
                            )
            elif kind == "episode":
                from core.models import Item
                ep_item = Item.objects.filter(
                    source="tmdb", media_type=MediaType.EPISODE.value,
                    media_id=tmdb_id, season_number=sn, episode_number=en,
                ).first()
                if ep_item:
                    for entry, watched_at in sorted(
                        zip(bucket["entries"], bucket["dates"], strict=True),
                        key=lambda pair: pair[1] or datetime.min.replace(tzinfo=UTC),
                    ):
                        if watched_at:
                            services.record_completion(
                                user=user, item=ep_item,
                                completed_at=watched_at,
                                source="trakt", source_event_id=str(entry["id"]),
                            )
                else:
                    needs_review.append(
                        {"reason": "episode missing after season fetch",
                         "entry": bucket["entry"]}
                    )
        except ProviderError as exc:
            needs_review.append({"reason": f"provider error: {exc}", "entry": bucket["entry"]})

        job.processed += plays
        job.save_progress()

    # Process every rating independently. Ratings need not have a matching
    # history entry, and each fetched rating must advance job accounting once.
    for rating_entry in ratings:
        try:
            item = _resolve_item_from_trakt_entry(rating_entry)
            if item is None:
                needs_review.append({"reason": "rating row not resolvable", "entry": rating_entry})
            else:
                services.update_tracking(
                    user=user,
                    item=item,
                    score=rating_entry.get("rating"),
                    bump_progressed_at=False,
                )
        except ProviderError as exc:
            needs_review.append({"reason": f"rating error: {exc}", "entry": rating_entry})
        job.processed += 1

    # Ratings create planning rows when none exist. A rated show with watched
    # episodes has already started, even when it has no watchlist entry.
    from tracking.models import TV, Episode
    for row in TV.objects.filter(user=user, status=Status.PLANNING.value):
        if Episode.objects.filter(
            user=user, item__parent__parent_id=row.item_id,
            status=Status.COMPLETED.value,
        ).exists():
            row.status = Status.IN_PROGRESS.value
            row.progressed_at = timezone.now()
            row.save()

    # Watchlist → status=planning on each item.
    for entry in watchlist:
        try:
            item = _resolve_item_from_trakt_entry(entry)
            if item is None:
                needs_review.append({"reason": "watchlist row not resolvable", "entry": entry})
            else:
                model = services.MODEL_FOR_MEDIA_TYPE[item.media_type]
                existing = model.objects.filter(user=user, item=item).first()
                # Watchlist membership must not demote something already started.
                from tracking.models import Episode
                has_watched_episodes = (
                    item.media_type == MediaType.TV.value
                    and Episode.objects.filter(
                        user=user, item__parent__parent=item,
                        status=Status.COMPLETED.value,
                    ).exists()
                )
                if not has_watched_episodes and (
                    existing is None or existing.status == Status.PLANNING.value
                ):
                    services.update_tracking(
                        user=user, item=item,
                        status=Status.PLANNING.value,
                        bump_progressed_at=False,
                    )
        except ProviderError as exc:
            needs_review.append({"reason": f"watchlist error: {exc}", "entry": entry})
        job.processed += 1
        job.save_progress()

    # Trakt user lists no longer map to anything — custom lists were removed.
    # Their entries were already folded into the catalog enrichment pass, so
    # only the progress accounting remains.
    for _lst_meta, items in list_items:
        job.processed += len(items)
        job.save_progress()

    job.needs_review = needs_review
    job.state = "done"
    job.phase = "done"
    job.finished_at = timezone.now()
    job.save()
    return job


def _resolve_item_from_trakt_entry(entry: dict) -> object | None:
    """Map a Trakt watchlist / list / history entry → core.Item.

    Handles movie, show, season, and episode shapes uniformly.
    """
    from core.models import Item

    kind = entry.get("type")
    if kind == "movie":
        tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
        if not tmdb_id:
            return None
        return Item.objects.filter(
            source="tmdb", media_type=MediaType.MOVIE.value, media_id=str(tmdb_id)
        ).first()
    if kind == "show":
        tmdb_id = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
        if not tmdb_id:
            return None
        return Item.objects.filter(
            source="tmdb", media_type=MediaType.TV.value, media_id=str(tmdb_id)
        ).first()
    if kind == "season":
        tmdb_id = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
        sn = (entry.get("season") or {}).get("number")
        if not (tmdb_id and sn is not None):
            return None
        return Item.objects.filter(
            source="tmdb", media_type=MediaType.SEASON.value,
            media_id=str(tmdb_id), season_number=sn,
        ).first()
    if kind == "episode":
        tmdb_id = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
        ep = entry.get("episode") or {}
        sn = ep.get("season")
        en = ep.get("number")
        if not (tmdb_id and sn is not None and en is not None):
            return None
        return Item.objects.filter(
            source="tmdb", media_type=MediaType.EPISODE.value,
            media_id=str(tmdb_id), season_number=sn, episode_number=en,
        ).first()
    return None
