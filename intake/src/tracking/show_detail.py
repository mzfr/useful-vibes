"""
Show-detail page context + writes.

Mirrors the season-page split: a read-side builder (`build_show_context`)
shared by the full page and every partial re-render, plus small write views
that all funnel back through it so `#show-body` always reflects the same
truth regardless of which control fired.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Favorite, Item

from .models import TV, Episode, ShowFeedPreference
from .season_views import _build_rows, _mark_episode_watched, _next_episode


@dataclass
class _SeasonSummary:
    number: int
    episode_count: int | None
    watched_count: int
    percent: int
    # "complete"   every known episode watched
    # "in_progress" some watched, some remaining
    # "new"        aired/materialized locally, nothing watched yet
    # "not_opened" TMDB knows this season exists but it's never been opened
    #              locally, so we don't have an accurate episode count yet
    status: str = "new"

    @property
    def remaining(self) -> int:
        return max((self.episode_count or 0) - self.watched_count, 0)


def _season_counts(season_items: list[Item], user) -> dict[int, tuple[int, int]]:
    """Return {season_pk: (episode_count, watched_count)} in two queries."""
    totals = dict(
        Item.objects.filter(parent__in=season_items, media_type=MediaType.EPISODE.value)
        .values_list("parent_id")
        .annotate(n=Count("pk"))
    )
    watched = dict(
        Episode.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
            item__parent__in=season_items,
        )
        .values_list("item__parent_id")
        .annotate(n=Count("pk"))
    )
    return {
        season.pk: (totals.get(season.pk, 0), watched.get(season.pk, 0))
        for season in season_items
    }


def _season_summaries(show_item: Item, user) -> list[_SeasonSummary]:
    """One row per season: local materialized seasons first (accurate counts),
    then any season TMDB knows about but the user hasn't opened yet."""
    local_seasons = list(
        Item.objects.filter(parent=show_item, media_type=MediaType.SEASON.value)
        .order_by("season_number")
    )
    local_numbers = {s.season_number for s in local_seasons}

    # Counts only: episode rows are fetched per season when one is expanded.
    counts_by_season = _season_counts(local_seasons, user)

    summaries: list[_SeasonSummary] = []
    for s in local_seasons:
        if not s.season_number:  # skip specials (season 0)
            continue
        total, watched = counts_by_season.get(s.pk, (0, 0))
        if total and watched >= total:
            status = "complete"
        elif watched:
            status = "in_progress"
        else:
            status = "new"
        summaries.append(
            _SeasonSummary(
                number=s.season_number,
                episode_count=total or None,
                watched_count=watched,
                percent=round(100 * watched / total) if total else 0,
                status=status,
            )
        )

    today = timezone.localdate().isoformat()
    for raw_season in (show_item.raw or {}).get("seasons", []):
        sn = raw_season.get("season_number")
        air = raw_season.get("air_date")
        if not sn or sn in local_numbers or not air or air > today:
            continue
        summaries.append(
            _SeasonSummary(
                number=sn,
                episode_count=raw_season.get("episode_count"),
                watched_count=0,
                percent=0,
                status="not_opened",
            )
        )

    summaries.sort(key=lambda s: s.number)
    return summaries


def _recent_watches(show_item: Item, user, limit=3) -> list[dict]:
    from datetime import date as _date

    rows = (
        Episode.objects.filter(
            user=user, status=Status.COMPLETED.value, item__parent__parent=show_item,
        )
        .select_related("item")
        .order_by("-progressed_at")[:limit]
    )
    today = timezone.localdate()
    out = []
    for row in rows:
        it = row.item
        label = "—"
        if row.end_date:
            if row.end_date == today:
                label = "today"
            elif row.end_date == today - _date.resolution:
                label = "yesterday"
            else:
                label = row.end_date.strftime("%-d %b")
        code = f"S{it.season_number:02d}E{it.episode_number:02d}" if it.season_number else ""
        out.append({
            "episode": {"code": code, "title": it.title},
            "watched_at_label": label,
            "score": row.score,
        })
    return out


def mark_next_episode_watched(user, show_item: Item) -> None:
    """Mark the show's current next-up episode watched. No-op if none."""
    local_seasons = list(
        Item.objects.filter(parent=show_item, media_type=MediaType.SEASON.value)
        .order_by("season_number")
    )
    for season_item in local_seasons:
        rows = _build_rows(season_item, user)
        candidate = _next_episode(rows)
        if candidate is not None:
            ep_item = Item.objects.get(
                parent=season_item, media_type=MediaType.EPISODE.value,
                episode_number=candidate.episode_number,
            )
            _mark_episode_watched(user, ep_item, watched_on=None)
            return


def build_show_context(user, show_item: Item) -> dict:
    tracking = TV.objects.filter(user=user, item=show_item).first()
    feed_pref = ShowFeedPreference.objects.filter(user=user, show=show_item).first()

    seasons = _season_summaries(show_item, user)
    watched_total = sum(s.watched_count for s in seasons)
    episode_total = sum(s.episode_count or 0 for s in seasons)

    # Next unwatched aired episode across every local season, earliest first.
    # `_EpisodeRow` already carries season_number/episode_number, enough to
    # build the toggle URL without an extra field.
    next_episode = None
    local_seasons = list(
        Item.objects.filter(parent=show_item, media_type=MediaType.SEASON.value)
        .order_by("season_number")
    )
    for season_item in local_seasons:
        rows = _build_rows(season_item, user)
        candidate = _next_episode(rows)
        if candidate is not None:
            next_episode = candidate
            break

    return {
        "show": show_item,
        "seasons": seasons,
        "tracking": tracking,
        "is_favorite": Favorite.objects.filter(user=user, item=show_item).exists(),
        "status_choices": Status.choices,
        "next_episode": next_episode,
        "watched_total": watched_total,
        "episode_total": episode_total,
        "recent_watches": _recent_watches(show_item, user),
        "visibility_toggles": [
            {
                "field": "up_next",
                "label": "Discontinued",
                "on": bool(feed_pref and feed_pref.up_next_hidden_at),
            },
            {
                "field": "releases",
                "label": "Show in Releases",
                "on": not (feed_pref and feed_pref.releases_hidden_at),
            },
        ],
    }
