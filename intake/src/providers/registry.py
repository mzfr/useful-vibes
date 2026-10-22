"""
Maps `source -> Provider instance`. Adding a source is one file + one line here.

Providers are lazily instantiated so missing credentials (e.g. TMDB_API_KEY in a
test where you only need Open Library) don't break unrelated paths.

Every provider returned to callers is wrapped in `CachedProvider` so repeated
`details()` / `season()` / `search()` calls within the cache TTL don't hit the
upstream API. Importers that need fresh data can reach `.inner` to bypass.
"""
from __future__ import annotations

from functools import cache

from .base import MediaType, NotSupported, Provider
from .cache import CachedProvider, clear_provider_cache
from .openlibrary import OpenLibraryProvider
from .tmdb import TMDBProvider

_PROVIDERS = {
    "tmdb": TMDBProvider,
    "openlibrary": OpenLibraryProvider,
}


@cache
def get(source: str) -> Provider:
    try:
        provider_class = _PROVIDERS[source]
    except KeyError as exc:
        raise NotSupported(f"Unknown provider source: {source!r}") from exc
    return CachedProvider(provider_class())


def for_media_type(media_type: str) -> Provider:
    """Return the canonical provider for a media type.

    Movies/TV/seasons/episodes → TMDB; books → Open Library.
    """
    if media_type in (MediaType.MOVIE.value, MediaType.TV.value,
                      MediaType.SEASON.value, MediaType.EPISODE.value):
        return get("tmdb")
    if media_type == MediaType.BOOK.value:
        return get("openlibrary")
    raise NotSupported(f"No provider for media_type={media_type!r}")


def all_sources() -> list[str]:
    return list(_PROVIDERS)


# Test/admin hook: drop the cached instances AND the provider response cache so
# re-configured settings take effect and respx mocks aren't poisoned by an
# earlier test run's payload.
def reset_cache() -> None:
    get.cache_clear()
    clear_provider_cache()


__all__ = ["all_sources", "for_media_type", "get", "reset_cache"]
