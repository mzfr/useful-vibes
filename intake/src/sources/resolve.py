"""
Resolve an intake Item to the identifiers source addons understand.

Stremio addons key on IMDb IDs: `tt…` for movies, `tt…:s:e` for episodes.
Intake stores external IDs on the show/movie Item's `raw` payload (fetched
from TMDB `append_to_response=external_ids`), so resolution walks up the
parent chain and reads the cached raw — no provider call, no network.
"""

from __future__ import annotations

from core.enums import MediaType
from core.models import Item


class NotResolvable(ValueError):
    """The item (or its parents) has no IMDb ID in the cached metadata."""


def imdb_id_for(item: Item) -> str:
    """IMDb ID for a movie, show, season, or episode Item."""
    target = item
    if item.media_type in (MediaType.EPISODE.value, MediaType.SEASON.value):
        show = item.parent
        while show is not None and show.media_type != MediaType.TV.value:
            show = show.parent
        if show is None:
            raise NotResolvable("Episode/season has no parent show.")
        target = show
    imdb = ((target.raw or {}).get("external_ids") or {}).get("imdb_id", "")
    if not imdb:
        raise NotResolvable(f"No IMDb ID cached for {target.title!r}.")
    return str(imdb)
