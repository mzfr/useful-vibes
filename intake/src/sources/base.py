"""
Normalized stream/seed result + the provider protocol for source lookup.

The first (and likely only) implementation speaks the Stremio addon
`/stream/{type}/{imdb_id}[:s:e]` protocol — Torrentio, MediaFusion, Comet and
friends all expose it. Swapping providers is a settings change (the addon base
URL), not a code change; a genuinely different protocol would be a second
backend registered next to `StremioAddonBackend`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class Stream:
    """One selectable source for a movie or episode."""

    title: str  # release name, single line, emoji metadata stripped
    info_hash: str  # 40-char hex; empty for non-torrent (debrid) streams
    url: str = ""  # direct URL when the addon provides one
    seeders: int | None = None
    size_mb: float | None = None
    quality: str = ""  # 2160p / 1080p / 720p … when parseable

    @property
    def size_display(self) -> str:
        """Human size: MB under 1 GB, GB above (with sensible precision)."""
        if self.size_mb is None:
            return ""
        if self.size_mb >= 1024:
            gb = self.size_mb / 1024
            return f"{gb:.1f} GB" if gb >= 10 else f"{gb:.2f} GB"
        return f"{self.size_mb:.0f} MB"


class StreamProviderError(RuntimeError):
    """Raised when a source lookup fails or returns an unusable payload."""


class NotConfigured(StreamProviderError):
    """Raised when no source addon URL is configured."""


@runtime_checkable
class StreamProvider(Protocol):
    def streams(
        self,
        media_type: str,
        imdb_id: str,
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> list[Stream]: ...


def magnet_link(stream: Stream, display_name: str | None = None) -> str:
    return (
        f"magnet:?xt=urn:btih:{stream.info_hash.lower()}"
        f"&dn={display_name or stream.title or 'Intake'}"
    )
