"""
"Up Next" feed — the heart of the home page.

For each TV show the user is engaged with, find:
  - the most recently progressed episode (drives ordering = least stale first
    from the user's perspective; ryot uses the same heuristic)
  - the next unwatched episode whose air_date is <= today (or null)

For movies and books we surface anything in IN_PROGRESS as a simpler row.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item

from .models import Book, Episode, Movie, ShowAutomaticDownload, ShowFeedPreference


@dataclass
class UpNextShowRow:
    show_item: Item
    season_item: Item | None = None
    next_episode: Item | None = None
    last_watched_at: object | None = None  # datetime | None
    source: str = ""
    unwatched_count: int = 0  # aired-and-unwatched episodes remaining
    new_season_count: int = 0  # seasons available but not yet in local catalog
    target_season_number: int | None = None  # season to link to (next ep's or newest new)
    automatic_download_enabled: bool = False

    @property
    def source_for_url(self) -> str:
        return self.show_item.source

    @property
    def media_id(self) -> str:
        return self.show_item.media_id

    @property
    def url(self) -> str:
        from django.urls import reverse

        if self.next_episode:
            return reverse(
                "tracking:episode_detail",
                args=(
                    self.show_item.media_id,
                    self.next_episode.season_number,
                    self.next_episode.episode_number,
                ),
            )
        if self.target_season_number:
            return reverse(
                "tracking:new_season_next_episode",
                args=(self.show_item.media_id, self.target_season_number),
            )
        return self.show_item.get_absolute_url()

    @property
    def aria_label(self) -> str:
        if self.next_episode:
            code = f"{self.next_episode.season_number:02d}E{self.next_episode.episode_number:02d}"
            return f"Open {self.show_item.title} {code}"
        if self.target_season_number:
            return f"Open {self.show_item.title} season {self.target_season_number} episode 1"
        return f"Open {self.show_item.title}"

    @property
    def automatic_download_url(self) -> str:
        from django.urls import reverse

        season_number = (
            self.next_episode.season_number if self.next_episode else self.target_season_number
        )
        episode_number = self.next_episode.episode_number if self.next_episode else 1
        if season_number is None or episode_number is None:
            return ""
        return reverse(
            "tracking:up_next_automatic_download",
            args=(self.show_item.pk, season_number, episode_number),
        )


@dataclass
class UpNextMovieRow:
    item: Item
    tracking: Movie


@dataclass
class UpNextBookRow:
    item: Item
    tracking: Book

    @property
    def progress_label(self) -> str:
        maximum = f"/{self.item.max_progress}" if self.item.max_progress else ""
        return f"{self.tracking.progress}{maximum} pages"


def shows_for_user(user) -> list[UpNextShowRow]:
    """Build the show rows ordered by staleness (most-recent progress first).

    Constant handful of queries regardless of library size. Uses values() rows
    and in-memory grouping — full ORM instantiation over 16k episode items
    measured 10s, values() 25ms for the same data.
    """
    hidden_show_ids = set(
        ShowFeedPreference.objects.filter(user=user, up_next_hidden_at__isnull=False)
        .values_list("show_id", flat=True)
    )

    automatic_show_ids = set(
        ShowAutomaticDownload.objects.filter(user=user).values_list("show_id", flat=True)
    )

    engaged_show_ids = set(
        Episode.objects.filter(user=user)
        .values_list("item__parent__parent_id", flat=True)
    )
    engaged_show_ids.discard(None)
    engaged_show_ids -= hidden_show_ids
    if not engaged_show_ids:
        return []

    watched_item_ids = set(
        Episode.objects.filter(user=user, status=Status.COMPLETED.value)
        .values_list("item_id", flat=True)
    )

    # Lightweight episode rows for every engaged show.
    ep_rows = list(
        Item.objects.filter(
            media_type=MediaType.EPISODE.value, parent__parent_id__in=engaged_show_ids
        ).values("id", "parent_id", "season_number", "episode_number", "release_date")
    )
    season_ids = {r["parent_id"] for r in ep_rows if r["parent_id"]}
    season_to_show = dict(
        Item.objects.filter(pk__in=season_ids).values_list("pk", "parent_id")
    )

    per_show_unwatched: dict[int, list[dict]] = {}
    for row in ep_rows:
        show_id = season_to_show.get(row["parent_id"])
        if show_id is None or row["id"] in watched_item_ids:
            continue
        per_show_unwatched.setdefault(show_id, []).append(row)

    # Most-recent progress per show. A completed episode always lands the show
    # here (even with progressed_at=None) — Up Next then sinks it to the end.
    last_watched: dict[int, object] = {}
    for row in (
        Episode.objects.filter(
            user=user, status=Status.COMPLETED.value,
            item__parent__parent_id__in=engaged_show_ids,
        ).values("item__parent__parent_id", "progressed_at")
    ):
        show_id = row["item__parent__parent_id"]
        ts = row["progressed_at"]
        last_watched.setdefault(show_id, None)
        if ts is not None and (last_watched[show_id] is None or ts > last_watched[show_id]):
            last_watched[show_id] = ts

    today = timezone.localdate()
    chosen: list[tuple[int, dict]] = []
    for show_id, unwatched in per_show_unwatched.items():
        if show_id not in last_watched:
            continue
        unwatched.sort(key=lambda r: (r["season_number"] or 0, r["episode_number"] or 0))
        pick = next(
            (r for r in unwatched if r["release_date"] is None or r["release_date"] <= today),
            None,
        )
        if pick is not None:
            chosen.append((show_id, pick))

    # Only the rows we display get full ORM objects: ~shows + ~chosen episodes.
    show_map = {s.pk: s for s in Item.objects.filter(pk__in=engaged_show_ids)}
    ep_map = {e.pk: e for e in Item.objects.filter(
        pk__in=[r["id"] for _, r in chosen]
    ).select_related("parent")}

    today = timezone.localdate()
    rows = []

    # Shows with a specific next unwatched episode.
    for show_id, pick in chosen:
        next_ep = ep_map[pick["id"]]
        unwatched_count = len(per_show_unwatched.get(show_id, []))
        rows.append(
            UpNextShowRow(
                show_item=show_map[show_id],
                season_item=next_ep.parent,
                next_episode=next_ep,
                last_watched_at=last_watched.get(show_id),
                source=show_map[show_id].source,
                unwatched_count=unwatched_count,
                target_season_number=next_ep.season_number,
                automatic_download_enabled=show_id in automatic_show_ids,
            )
        )

    # Shows where all aired episodes are watched but new seasons exist in
    # TMDB's cached raw payload. These get a row pointing at the show detail
    # page so the user can navigate to the new season.
    shows_with_next_ep = {show_id for show_id, _ in chosen}
    for show_id in engaged_show_ids:
        if show_id in shows_with_next_ep or show_id not in last_watched:
            continue
        show = show_map.get(show_id)
        if show is None:
            continue
        local_season_numbers = set(
            Item.objects.filter(parent=show, media_type=MediaType.SEASON.value)
            .values_list("season_number", flat=True)
        )
        new_count = 0
        newest_new_season = None
        for s in (show.raw or {}).get("seasons", []):
            sn = s.get("season_number")
            air = s.get("air_date")
            if sn and sn not in local_season_numbers and air and air <= today.isoformat():
                new_count += 1
                if newest_new_season is None or sn > newest_new_season:
                    newest_new_season = sn
        if new_count > 0:
            rows.append(
                UpNextShowRow(
                    show_item=show,
                    last_watched_at=last_watched.get(show_id),
                    source=show.source,
                    new_season_count=new_count,
                    target_season_number=newest_new_season,
                    automatic_download_enabled=show_id in automatic_show_ids,
                )
            )

    # Sentinel must be a tz-aware datetime — mixing datetime and int in one
    # sort key raises TypeError as soon as any progressed_at is NULL.
    epoch = datetime.min.replace(tzinfo=UTC)
    rows.sort(key=lambda r: r.last_watched_at or epoch, reverse=True)
    return rows


def in_progress_movies(user) -> list[UpNextMovieRow]:
    qs = Movie.objects.filter(user=user, status=Status.IN_PROGRESS.value).select_related("item")
    return [UpNextMovieRow(item=m.item, tracking=m) for m in qs]


def in_progress_books(user) -> list[UpNextBookRow]:
    qs = Book.objects.filter(user=user, status=Status.IN_PROGRESS.value).select_related("item")
    return [UpNextBookRow(item=b.item, tracking=b) for b in qs]
