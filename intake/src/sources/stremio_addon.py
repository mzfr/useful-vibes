"""
Stremio addon stream backend — the protocol Torrentio, MediaFusion, Comet etc.
all speak: `GET {base}/stream/{movie|series}/{imdb_id}[:s:e].json`.

Request headers mirror a Stremio web client (some addons, Torrentio included,
sit behind Cloudflare and reject bare client UAs). Streams without an
infoHash (debrid/web hosted) are skipped — only torrents a download client
can consume are returned.

This is the only torrent-specific file in the app; it exists so every other
module can talk about "sources" without leaking the word.
"""

from __future__ import annotations

import re

import httpx

from core.settings_store import source_addon_url

from .base import Stream, StreamProviderError

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://stremio-web.vercel.app/",
    "Origin": "https://stremio-web.vercel.app",
}

# Torrentio appends emoji metadata lines to the release title:
# "Release.Name 1080p\n● 42\n💾 3.2 GB ...". Parse them, keep the first line.
# Seeders have been seen as both 👤 (U+1F465) and ● (U+25CF) — accept either.
# (Real emoji required in the pattern: surrogate-pair escapes do NOT combine
# into the actual character in Python and will silently never match.)
_SEEDERS_RE = re.compile("[●👤]\\s*(\\d+)")
_SIZE_RE = re.compile("💾\\s*([\\d.]+)\\s*(MB|GB)", re.IGNORECASE)
_QUALITY_RE = re.compile(r"(2160p|1080p|720p|480p|360p|240p)", re.IGNORECASE)


def _clean_title(title: str) -> str:
    return title.split("\n", 1)[0].strip()


# ---------------------------------------------------------------- relevance
# Release names drop punctuation and vary separators, so compare on a
# normalized form: lowercase, non-alphanumeric runs collapsed to one space.
_NORM_RE = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    return _NORM_RE.sub(" ", text.lower()).strip()


# Space-padded so matches land on word boundaries — "sunny" won't false-hit
# inside "sunnya", and short words still need real token hits.
def _pad(s: str) -> str:
    return f" {s} "


def is_relevant(release_title: str, item_title: str | None) -> bool:
    """False only when the release is clearly for something else.

    Exact rule: if the normalized full item title appears in the release
    string, keep. Otherwise take the item title's longest word (e.g.
    "philadelphia") and require it to appear. Mega-packs like
    "Marathi Movies & Plays Mega Pack" fail both and get dropped.
    """
    if not item_title:
        return True
    haystack = _pad(_normalize(release_title))
    if _pad(_normalize(item_title)) in haystack:
        return True
    words = re.findall(r"[a-z0-9]+", item_title.lower())
    if not words:
        return True
    longest = max(words, key=len)
    return _pad(_normalize(longest)) in haystack


def _parse_stream(raw: dict) -> Stream | None:
    title = raw.get("title", "")
    info_hash = (raw.get("infoHash") or "").strip().lower()
    if not info_hash:
        return None  # debrid/web stream — not something a download client takes
    seeders = None
    if m := _SEEDERS_RE.search(title):
        seeders = int(m.group(1))
    size_mb = None
    if m := _SIZE_RE.search(title):
        value = float(m.group(1))
        size_mb = value * 1024 if m.group(2).upper() == "GB" else value
    quality = ""
    if m := _QUALITY_RE.search(title):
        quality = m.group(1)
    return Stream(
        title=_clean_title(title) or raw.get("name", "Unknown release"),
        info_hash=info_hash,
        url=raw.get("url", ""),
        seeders=seeders,
        size_mb=size_mb,
        quality=quality,
    )


class StremioAddonBackend:
    """Stream lookup against any Stremio-compatible addon base URL."""

    def streams(
        self,
        media_type: str,
        imdb_id: str,
        season_number: int | None = None,
        episode_number: int | None = None,
        *,
        quality: str | None = None,
        query: str | None = None,
    ) -> list[Stream]:
        base = source_addon_url().rstrip("/")
        if not base:
            raise StreamProviderError("No source addon URL configured.")

        if media_type == "movie":
            path = f"/stream/movie/{imdb_id}.json"
        elif media_type == "series" and season_number and episode_number:
            path = f"/stream/series/{imdb_id}:{season_number}:{episode_number}.json"
        else:
            raise StreamProviderError("Series lookups need both a season and an episode number.")

        try:
            resp = httpx.get(base + path, headers=_HEADERS, timeout=15.0)
        except httpx.HTTPError as exc:
            raise StreamProviderError(f"Source addon unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise StreamProviderError(f"Source addon returned {resp.status_code} for {path}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise StreamProviderError("Source addon returned a non-JSON body") from exc

        streams = [
            s
            for s in (_parse_stream(raw) for raw in payload.get("streams", []))
            if s is not None
            and (quality is None or s.quality == quality)
            and is_relevant(s.title, query)
        ]
        # Most seeds first; unparseable counts rank below known-good ones.
        streams.sort(key=lambda s: s.seeders if s.seeders is not None else -1, reverse=True)
        return streams
