"""
Caching wrapper around any `Provider`.

We cache the normalized `Metadata` dataclasses (not the raw HTTP responses) so
the cached path returns the same shape the views expect. Cache keys are
`(source, kind, *args)` namespaced under the `providers` cache.

Why a wrapper instead of decorating Provider methods directly:
  - Keeps the raw provider classes pure → respx tests stay deterministic
    without needing to flush a process-wide cache.
  - The TTL per call type (details / season / episode / search) lives in
    settings, not scattered across providers.

Why pickling: dataclasses with `slots=True` aren't trivially JSON-serializable
(date objects, nested Person/Episode dataclasses), and locmem stores Python
objects directly anyway — pickling adds resilience if the backend ever swaps.
"""
from __future__ import annotations

import hashlib
from typing import Any

from django.conf import settings
from django.core.cache import caches

from .base import (
    EpisodeMetadata,
    Metadata,
    Provider,
    SearchResults,
    SeasonMetadata,
)

_PROVIDER_CACHE = "providers"


def _key(source: str, kind: str, *parts: Any) -> str:
    raw = "|".join([source, kind, *(str(p) for p in parts)])
    # Hash long search queries so we don't blow past memcached key limits if
    # someone later swaps the backend.
    return "prov:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ttl(kind: str) -> int:
    return settings.PROVIDER_CACHE_TTLS.get(kind, 3600)


class CachedProvider:
    """Drop-in Provider wrapper that memoizes calls in the `providers` cache.

    Implements the same surface as `providers.base.Provider` so the registry
    can return it transparently. Bypassable by calling `.inner` directly
    (used by importers that want fresh data).
    """

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.source = inner.source
        self.supported_types = inner.supported_types
        self._cache = caches[_PROVIDER_CACHE]

    # ------------------------------------------------------------------ search

    def search(self, media_type: str, query: str, page: int = 1) -> SearchResults:
        k = _key(self.source, "search", media_type, query.strip().lower(), page)
        hit = self._cache.get(k)
        if hit is not None:
            return hit
        result = self.inner.search(media_type, query, page=page)
        self._cache.set(k, result, _ttl("search"))
        return result

    # ----------------------------------------------------------------- details

    def details(self, media_type: str, media_id: str) -> Metadata:
        k = _key(self.source, "details", media_type, media_id)
        hit = self._cache.get(k)
        if hit is not None:
            return hit
        result = self.inner.details(media_type, media_id)
        self._cache.set(k, result, _ttl("details"))
        return result

    def season(self, media_id: str, season_number: int) -> SeasonMetadata:
        k = _key(self.source, "season", media_id, season_number)
        hit = self._cache.get(k)
        if hit is not None:
            return hit
        result = self.inner.season(media_id, season_number)
        self._cache.set(k, result, _ttl("season"))
        return result

    def episode(
        self, media_id: str, season_number: int, episode_number: int
    ) -> EpisodeMetadata:
        k = _key(self.source, "episode", media_id, season_number, episode_number)
        hit = self._cache.get(k)
        if hit is not None:
            return hit
        result = self.inner.episode(media_id, season_number, episode_number)
        self._cache.set(k, result, _ttl("episode"))
        return result

    # ----------------------------------------------------- cache management

    def invalidate(self, media_type: str, media_id: str) -> None:
        """Drop the details cache for a single item (used after a write that
        the user expects to see immediately — none yet, but the hook is here)."""
        self._cache.delete(_key(self.source, "details", media_type, media_id))


def clear_provider_cache() -> None:
    """Wipe the entire provider cache. Used by tests."""
    caches[_PROVIDER_CACHE].clear()
