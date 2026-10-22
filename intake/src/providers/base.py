"""
Provider protocol + the normalized dataclasses every provider returns.

Every provider module returns the same shape so the UI never branches on source.
Provider-specific blanks stay `None` / empty list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable

from core.enums import MediaType


@dataclass(slots=True)
class SearchHit:
    source: str
    media_type: str
    media_id: str
    title: str
    image: str = ""
    year: int | None = None
    synopsis: str = ""
    runtime: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchResults:
    hits: list[SearchHit]
    page: int = 1
    total_pages: int = 1
    total_results: int = 0


@dataclass(slots=True)
class Person:
    name: str
    role: str = ""          # "Director", "Writer", character name for cast, etc.
    image: str = ""


@dataclass(slots=True)
class Metadata:
    source: str
    media_type: str
    media_id: str
    title: str
    image: str = ""
    synopsis: str = ""
    genres: list[str] = field(default_factory=list)
    release_date: date | None = None
    runtime: int | None = None            # minutes for movies/episodes; total page count for books
    max_progress: int | None = None       # episodes-in-season, pages-in-book, 1 for movies
    external_ids: dict[str, str] = field(default_factory=dict)
    cast: list[Person] = field(default_factory=list)
    crew: list[Person] = field(default_factory=list)
    # Raw provider payload — stashed verbatim on Item.raw for later enrichment
    # without re-fetching. UI must never read from this; that's what the typed
    # fields above are for.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SeasonMetadata(Metadata):
    season_number: int = 0
    episodes: list[EpisodeMetadata] = field(default_factory=list)


@dataclass(slots=True)
class EpisodeMetadata(Metadata):
    season_number: int = 0
    episode_number: int = 0


@runtime_checkable
class Provider(Protocol):
    source: str
    supported_types: set[str]

    def search(self, media_type: str, query: str, page: int = 1) -> SearchResults: ...
    def details(self, media_type: str, media_id: str) -> Metadata: ...
    def season(self, media_id: str, season_number: int) -> SeasonMetadata: ...
    def episode(
        self, media_id: str, season_number: int, episode_number: int
    ) -> EpisodeMetadata: ...


class ProviderError(RuntimeError):
    """Raised when a provider call fails or returns unexpected shape."""


class NotSupported(ProviderError):
    """Raised when a provider does not support the requested media type."""


def _ensure(provider: Provider, media_type: str) -> None:
    if media_type not in provider.supported_types:
        raise NotSupported(
            f"{provider.source!r} does not support media_type={media_type!r}"
        )


# Re-export MediaType so callers can `from providers.base import MediaType`.
__all__ = [
    "EpisodeMetadata",
    "MediaType",
    "Metadata",
    "NotSupported",
    "Person",
    "Provider",
    "ProviderError",
    "SearchHit",
    "SearchResults",
    "SeasonMetadata",
    "_ensure",
]
