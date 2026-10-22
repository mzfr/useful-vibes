"""
TMDB provider — movies, shows, seasons, episodes.

Auth: v3 API key passed as `api_key` query param (works for personal use without
the v4 bearer-token rotation hassle). Set TMDB_API_KEY in env.

We deliberately only normalize the fields the UI uses today, but stash the full
payload in `Metadata.raw` so adding fields later is no schema change.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.enums import MediaType
from core.settings_store import tmdb_api_key

from .base import (
    EpisodeMetadata,
    Metadata,
    Person,
    Provider,
    ProviderError,
    SearchHit,
    SearchResults,
    SeasonMetadata,
    _ensure,
)
from .http import HTTPClient

_IMG_BASE = "https://image.tmdb.org/t/p"


def _img(path: str | None, size: str = "w500") -> str:
    if not path:
        return ""
    return f"{_IMG_BASE}/{size}{path}"


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


class TMDBProvider(Provider):
    source = "tmdb"
    supported_types = {MediaType.MOVIE.value, MediaType.TV.value}

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key if api_key is not None else tmdb_api_key()
        if not key:
            raise ProviderError("TMDB_API_KEY is not configured.")
        self._http = HTTPClient(
            base_url="https://api.themoviedb.org/3",
            params={"api_key": key, "language": "en-US"},
        )

    # ------------------------------------------------------------------ search

    def search(self, media_type: str, query: str, page: int = 1) -> SearchResults:
        _ensure(self, media_type)
        endpoint = "/search/movie" if media_type == MediaType.MOVIE.value else "/search/tv"
        data = self._http.get_json(endpoint, query=query, page=page, include_adult="false")
        hits = [self._search_hit(media_type, r) for r in data.get("results", [])]
        return SearchResults(
            hits=hits,
            page=data.get("page", page),
            total_pages=data.get("total_pages", 1),
            total_results=data.get("total_results", len(hits)),
        )

    def _search_hit(self, media_type: str, row: dict[str, Any]) -> SearchHit:
        if media_type == MediaType.MOVIE.value:
            title = row.get("title") or row.get("original_title") or ""
            release = row.get("release_date") or ""
        else:
            title = row.get("name") or row.get("original_name") or ""
            release = row.get("first_air_date") or ""
        year = None
        if release[:4].isdigit():
            year = int(release[:4])
        return SearchHit(
            source=self.source,
            media_type=media_type,
            media_id=str(row.get("id")),
            title=title,
            image=_img(row.get("poster_path"), "w342"),
            year=year,
            synopsis=row.get("overview", "") or "",
        )

    # ----------------------------------------------------------------- details

    def details(self, media_type: str, media_id: str) -> Metadata:
        _ensure(self, media_type)
        if media_type == MediaType.MOVIE.value:
            return self._movie_details(media_id)
        return self._tv_details(media_id)

    def _movie_details(self, media_id: str) -> Metadata:
        raw = self._http.get_json(
            f"/movie/{media_id}",
            append_to_response="credits,external_ids",
        )
        return Metadata(
            source=self.source,
            media_type=MediaType.MOVIE.value,
            media_id=str(raw.get("id")),
            title=raw.get("title") or raw.get("original_title") or "",
            image=_img(raw.get("poster_path")),
            synopsis=raw.get("overview", "") or "",
            genres=[g["name"] for g in raw.get("genres", []) if g.get("name")],
            release_date=_parse_date(raw.get("release_date")),
            runtime=raw.get("runtime"),
            max_progress=1,
            external_ids=_pluck_external_ids(raw.get("external_ids", {})),
            cast=_cast(raw.get("credits", {}).get("cast", [])),
            crew=_crew(raw.get("credits", {}).get("crew", [])),
            raw=raw,
        )

    def _tv_details(self, media_id: str) -> Metadata:
        raw = self._http.get_json(
            f"/tv/{media_id}",
            append_to_response="credits,external_ids",
        )
        return Metadata(
            source=self.source,
            media_type=MediaType.TV.value,
            media_id=str(raw.get("id")),
            title=raw.get("name") or raw.get("original_name") or "",
            image=_img(raw.get("poster_path")),
            synopsis=raw.get("overview", "") or "",
            genres=[g["name"] for g in raw.get("genres", []) if g.get("name")],
            release_date=_parse_date(raw.get("first_air_date")),
            runtime=(raw.get("episode_run_time") or [None])[0],
            max_progress=raw.get("number_of_episodes"),
            external_ids=_pluck_external_ids(raw.get("external_ids", {})),
            cast=_cast(raw.get("credits", {}).get("cast", [])),
            crew=_crew(raw.get("credits", {}).get("crew", [])),
            raw=raw,
        )

    # ------------------------------------------------------------- season / ep

    def season(self, media_id: str, season_number: int) -> SeasonMetadata:
        raw = self._http.get_json(f"/tv/{media_id}/season/{season_number}")
        episodes = [
            EpisodeMetadata(
                source=self.source,
                media_type=MediaType.EPISODE.value,
                media_id=str(media_id),
                title=ep.get("name") or "",
                image=_img(ep.get("still_path"), "w300"),
                synopsis=ep.get("overview", "") or "",
                release_date=_parse_date(ep.get("air_date")),
                runtime=ep.get("runtime"),
                max_progress=1,
                season_number=season_number,
                episode_number=ep.get("episode_number") or 0,
                raw=ep,
            )
            for ep in raw.get("episodes", [])
        ]
        return SeasonMetadata(
            source=self.source,
            media_type=MediaType.SEASON.value,
            media_id=str(media_id),
            title=raw.get("name") or f"Season {season_number}",
            image=_img(raw.get("poster_path")),
            synopsis=raw.get("overview", "") or "",
            release_date=_parse_date(raw.get("air_date")),
            max_progress=len(episodes),
            season_number=season_number,
            episodes=episodes,
            raw=raw,
        )

    def episode(
        self, media_id: str, season_number: int, episode_number: int
    ) -> EpisodeMetadata:
        raw = self._http.get_json(
            f"/tv/{media_id}/season/{season_number}/episode/{episode_number}"
        )
        return EpisodeMetadata(
            source=self.source,
            media_type=MediaType.EPISODE.value,
            media_id=str(media_id),
            title=raw.get("name") or "",
            image=_img(raw.get("still_path"), "w300"),
            synopsis=raw.get("overview", "") or "",
            release_date=_parse_date(raw.get("air_date")),
            runtime=raw.get("runtime"),
            max_progress=1,
            season_number=season_number,
            episode_number=episode_number,
            raw=raw,
        )


# ---------------------------------------------------------------------- helpers


def _pluck_external_ids(payload: dict[str, Any]) -> dict[str, str]:
    keep = ("imdb_id", "tvdb_id", "tvrage_id", "facebook_id", "instagram_id", "twitter_id")
    return {k: str(payload[k]) for k in keep if payload.get(k)}


def _cast(rows: list[dict[str, Any]], limit: int = 12) -> list[Person]:
    out = []
    for r in rows[:limit]:
        out.append(
            Person(
                name=r.get("name", ""),
                role=r.get("character", "") or "",
                image=_img(r.get("profile_path"), "w185"),
            )
        )
    return out


def _crew(rows: list[dict[str, Any]]) -> list[Person]:
    # Keep only the headline credits — directors, writers, creators.
    keep_jobs = {"Director", "Writer", "Screenplay", "Creator", "Showrunner"}
    out = []
    for r in rows:
        if r.get("job") in keep_jobs:
            out.append(
                Person(
                    name=r.get("name", ""),
                    role=r.get("job", ""),
                    image=_img(r.get("profile_path"), "w185"),
                )
            )
    return out
