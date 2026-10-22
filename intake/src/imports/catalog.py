"""Plan and durably enrich the catalog required by a Trakt export."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from django.db import transaction

from core.enums import MediaType
from core.models import Item
from providers import registry
from providers.base import ProviderError
from tracking import services


@dataclass(frozen=True)
class CatalogPlan:
    roots: frozenset[tuple[str, str]]  # (media_type, tmdb_id)
    seasons: frozenset[tuple[str, int]]  # (show tmdb_id, season number)


def build_catalog_plan(history, ratings, watchlist, list_items) -> CatalogPlan:
    roots: set[tuple[str, str]] = set()
    seasons: set[tuple[str, int]] = set()
    entries = [*history, *ratings, *watchlist]
    for _meta, items in list_items:
        entries.extend(items)
    for entry in entries:
        kind = entry.get("type")
        if kind == "movie":
            mid = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
            if mid:
                roots.add((MediaType.MOVIE.value, str(mid)))
        elif kind in ("show", "season", "episode"):
            mid = ((entry.get("show") or {}).get("ids") or {}).get("tmdb")
            if not mid:
                continue
            mid = str(mid)
            roots.add((MediaType.TV.value, mid))
            if kind == "season":
                sn = (entry.get("season") or {}).get("number")
            elif kind == "episode":
                sn = (entry.get("episode") or {}).get("season")
            else:
                sn = None
            if sn is not None:
                seasons.add((mid, int(sn)))
    return CatalogPlan(frozenset(roots), frozenset(seasons))


class RateLimiter:
    def __init__(self, rate=20):
        self.interval = 1 / rate
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = max(0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)


def _retry(call, limiter, attempts=5):
    for attempt in range(attempts):
        limiter.wait()
        try:
            return call()
        except ProviderError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5 * (2 ** attempt))


def enrich_catalog(plan: CatalogPlan, *, workers=8, batch_size=20, progress=None) -> list[dict]:
    """Fetch only incomplete catalog entities; ORM access stays coordinator-only."""
    complete_roots = set(
        Item.objects.filter(
            source="tmdb", metadata_complete=True,
            media_type__in=[MediaType.MOVIE.value, MediaType.TV.value],
        ).values_list("media_type", "media_id")
    )
    complete_seasons = set(
        Item.objects.filter(
            source="tmdb", media_type=MediaType.SEASON.value, metadata_complete=True,
        ).values_list("media_id", "season_number")
    )
    missing_roots = sorted(plan.roots - complete_roots)
    missing_seasons = sorted(plan.seasons - complete_seasons)
    provider = registry.get("tmdb")
    limiter = RateLimiter()
    failures: list[dict] = []

    def fetch_root(task):
        mt, mid = task
        return "root", task, _retry(lambda: provider.details(mt, mid), limiter)

    # Seasons depend on persisted show roots, so fetch/write roots first.
    for tasks, fetcher in ((missing_roots, fetch_root),):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetcher, task) for task in tasks]
            batch = []
            for future in as_completed(futures):
                try:
                    batch.append(future.result())
                except ProviderError as exc:
                    failures.append({"reason": f"catalog details error: {exc}"})
                if len(batch) >= batch_size:
                    _persist_batch(batch)
                    if progress:
                        progress(len(batch))
                    batch.clear()
            if batch:
                _persist_batch(batch)
                if progress:
                    progress(len(batch))

    shows = {item.media_id: item for item in Item.objects.filter(
        source="tmdb", media_type=MediaType.TV.value,
        media_id__in=[mid for mid, _sn in missing_seasons],
    )}

    def fetch_season(task):
        mid, sn = task
        return "season", task, _retry(lambda: provider.season(mid, sn), limiter)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_season, task) for task in missing_seasons if task[0] in shows]
        batch = []
        for future in as_completed(futures):
            try:
                batch.append(future.result())
            except ProviderError as exc:
                failures.append({"reason": f"catalog season error: {exc}"})
            if len(batch) >= batch_size:
                _persist_batch(batch, shows)
                if progress:
                    progress(len(batch))
                batch.clear()
        if batch:
            _persist_batch(batch, shows)
            if progress:
                progress(len(batch))
    return failures


@transaction.atomic
def _persist_batch(batch, shows=None):
    for kind, task, payload in batch:
        if kind == "root":
            services.upsert_item(payload)
        else:
            mid, _sn = task
            services.upsert_season_with_episodes(payload, tv_item=shows[mid])
