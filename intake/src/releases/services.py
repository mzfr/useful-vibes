"""
Build the releases payload for a user.

Cache design: `ReleaseCheck.payload_json` stores ONLY provider-side facts
(seasons and their aired episodes), refreshed at most every 24h. The user's
watched-set is applied at *read* time — so marking an episode watched removes
it from the Releases page immediately instead of lingering for the TTL.

`seen_at` (drives the sidebar unread badge) resets whenever a refresh produces
a different payload than what was cached — genuinely new content re-arms the
badge even after the user acked older items.

For each TV show the user has tracked (any tracking row on the show or its
descendants):

  - new seasons:   released seasons the user has no engagement with
  - new episodes:  aired-but-unwatched episodes in seasons they HAVE engaged with

Movie sequels via TMDB collections: deliberately not implemented — coverage is
too patchy to be more than a placebo section.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import TV, Episode, Season, ShowFeedPreference

from .models import ReleaseCheck


@dataclass
class NewSeason:
    show: Item
    season_number: int
    air_date: str | None
    episode_count: int | None


@dataclass
class NewEpisode:
    show: Item
    season_number: int
    episode_number: int
    title: str
    air_date: str | None


def _shows_tracked_by(user) -> list[Item]:
    """Every TV show the user has any engagement with."""
    show_ids: set[int] = set()
    engaged_statuses = [Status.IN_PROGRESS.value, Status.COMPLETED.value]
    show_ids.update(
        TV.objects.filter(user=user, status__in=engaged_statuses)
        .values_list("item_id", flat=True)
    )
    show_ids.update(
        Season.objects.filter(user=user, status__in=engaged_statuses)
        .values_list("item__parent_id", flat=True)
    )
    show_ids.update(
        Episode.objects.filter(user=user, status__in=engaged_statuses)
        .values_list("item__parent__parent_id", flat=True)
    )
    show_ids.discard(None)
    hidden = ShowFeedPreference.objects.filter(
        user=user, releases_hidden_at__isnull=False
    ).values_list("show_id", flat=True)
    return list(
        Item.objects.filter(pk__in=show_ids, media_type=MediaType.TV.value)
        .exclude(pk__in=hidden)
    )


def _fetch_provider_payload(show: Item, engaged_seasons: set[int]) -> dict:
    """Build the release payload from local catalog data. No TMDB calls.

    The import already fetched and persisted all season/episode metadata.
    This function reads it back from the Item table. If a season's episodes
    are genuinely missing from the DB (incomplete import), that season is
    silently skipped — it will appear on the next enrichment run.
    """
    if show.source != "tmdb":
        return {"seasons": [], "episodes": {}}

    # Seasons from local catalog + any aired seasons known from the show's
    # raw TMDB payload that haven't been fetched yet (new seasons).
    season_items = list(
        Item.objects.filter(
            parent=show, media_type=MediaType.SEASON.value,
            metadata_complete=True,
        ).order_by("season_number")
    )
    local_season_numbers = {s.season_number for s in season_items}
    seasons = [
        {
            "season_number": s.season_number,
            "air_date": s.release_date.isoformat() if s.release_date else None,
            "episode_count": s.max_progress,
        }
        for s in season_items
        if s.season_number  # skip specials
    ]
    # Include aired-but-unfetched seasons from the show's cached TMDB raw.
    for s in (show.raw or {}).get("seasons", []):
        sn = s.get("season_number")
        if not sn or sn in local_season_numbers:
            continue
        air = s.get("air_date")
        if air and air <= timezone.localdate().isoformat():
            seasons.append({
                "season_number": sn,
                "air_date": air,
                "episode_count": s.get("episode_count"),
            })

    # Episodes from local catalog, only for engaged seasons.
    episodes: dict[str, list[dict]] = {}
    engaged_season_ids = [
        s.pk for s in season_items if s.season_number in engaged_seasons
    ]
    if engaged_season_ids:
        ep_rows = (
            Item.objects.filter(
                parent_id__in=engaged_season_ids,
                media_type=MediaType.EPISODE.value,
                metadata_complete=True,
            )
            .values("parent__season_number", "episode_number", "title", "release_date")
            .order_by("parent__season_number", "episode_number")
        )
        for row in ep_rows:
            sn_str = str(row["parent__season_number"])
            episodes.setdefault(sn_str, []).append({
                "episode_number": row["episode_number"],
                "title": row["title"],
                "air_date": row["release_date"].isoformat() if row["release_date"] else None,
            })

    return {"seasons": seasons, "episodes": episodes}


def _engaged_seasons(user, show: Item) -> set[int]:
    """Season numbers the user has any episode tracking under, plus any
    directly-tracked seasons."""
    engaged = set(
        Episode.objects.filter(user=user, item__parent__parent=show)
        .values_list("item__season_number", flat=True)
    )
    engaged.update(
        Season.objects.filter(user=user, item__parent=show)
        .values_list("item__season_number", flat=True)
    )
    engaged.discard(None)
    return engaged


def _show_engagement(user, show_ids) -> tuple[dict[int, set[int]], dict[int, set[tuple[int, int]]]]:
    """Batch-load per-show engagement for the whole feed in two queries.

    Returns (engaged_seasons[show_id], watched_episodes[show_id]) where the
    watched set contains (season_number, episode_number) pairs.
    """
    engaged: dict[int, set[int]] = {}
    watched: dict[int, set[tuple[int, int]]] = {}
    rows = (
        Episode.objects.filter(user=user, item__parent__parent_id__in=show_ids)
        .values_list("item__parent__parent_id", "item__season_number", "item__episode_number", "status")
    )
    for show_id, sn, en, status in rows:
        if sn is None:
            continue
        engaged.setdefault(show_id, set()).add(sn)
        if status == Status.COMPLETED.value and en is not None:
            watched.setdefault(show_id, set()).add((sn, en))
    rows = (
        Season.objects.filter(user=user, item__parent_id__in=show_ids)
        .values_list("item__parent_id", "item__season_number")
    )
    for show_id, sn in rows:
        if sn is not None:
            engaged.setdefault(show_id, set()).add(sn)
    return engaged, watched


def _diff_for_user(
    show: Item, payload: dict, engaged: set[int], watched: set[tuple[int, int]],
) -> tuple[list[NewSeason], list[NewEpisode]]:
    """Apply the user's CURRENT watched-state to the cached provider payload.

    `engaged`/`watched` come from `_show_engagement` — no per-call queries.
    """
    today_iso = timezone.localdate().isoformat()

    new_seasons = [
        NewSeason(
            show=show,
            season_number=s["season_number"],
            air_date=s.get("air_date"),
            episode_count=s.get("episode_count"),
        )
        for s in payload.get("seasons", [])
        if s["season_number"] not in engaged
        and s.get("air_date")
        and s["air_date"] <= today_iso
    ]

    new_episodes: list[NewEpisode] = []
    for sn_str, eps in payload.get("episodes", {}).items():
        sn = int(sn_str)
        for ep in eps:
            if (sn, ep["episode_number"]) in watched:
                continue
            air = ep.get("air_date")
            if not air or air > today_iso:
                continue
            new_episodes.append(
                NewEpisode(
                    show=show,
                    season_number=sn,
                    episode_number=ep["episode_number"],
                    title=ep.get("title", ""),
                    air_date=air,
                )
            )
    return new_seasons, new_episodes


def build_feed(user) -> dict:
    """Return {'new_seasons': [...], 'new_episodes': [...], 'unread_count': N}.

    The per-show loop used to issue ~3 queries each; engagement and cache rows
    are now batch-loaded so a full feed costs a handful of queries.
    """
    shows = _shows_tracked_by(user)
    if not shows:
        return {"new_seasons": [], "new_episodes": [], "unread_count": 0}

    show_ids = [s.pk for s in shows]
    engaged_map, watched_map = _show_engagement(user, show_ids)
    caches = {c.item_id: c for c in ReleaseCheck.objects.filter(user=user, item__in=shows)}

    all_new_seasons: list[NewSeason] = []
    all_new_episodes: list[NewEpisode] = []
    fresh_unread = 0

    for show in shows:
        cache = caches.get(show.pk)
        if cache is None:
            cache = ReleaseCheck.objects.create(user=user, item=show)
            caches[show.pk] = cache
        if not cache.is_fresh() or not cache.payload_json:
            fresh = _fetch_provider_payload(show, engaged_map.get(show.pk, set()))
            if fresh is not None:
                if fresh != cache.payload_json:
                    cache.seen_at = None
                cache.payload_json = fresh
                cache.last_checked_at = timezone.now()
                cache.save()

        seasons, episodes = _diff_for_user(
            show, cache.payload_json,
            engaged_map.get(show.pk, set()), watched_map.get(show.pk, set()),
        )
        all_new_seasons.extend(seasons)
        all_new_episodes.extend(episodes)

        if cache.seen_at is None and (seasons or episodes):
            fresh_unread += 1

    return {
        "new_seasons": all_new_seasons,
        "new_episodes": all_new_episodes,
        "unread_count": fresh_unread,
    }


def mark_all_seen(user) -> None:
    ReleaseCheck.objects.filter(user=user, seen_at__isnull=True).update(
        seen_at=timezone.now()
    )
