"""Offline Trakt import from the official export ZIP/folder.

Trakt restricted API access to VIP accounts, so the downloadable export is now
the only workable path. Everything here is filesystem-only — no OAuth, no
client_id/secret. TMDB is still required to resolve metadata.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.enums import MediaType
from providers import registry
from tracking import services

from .models import ImportJob, TraktLegacy


def _load(path: Path, name: str, default):
    target = path / name
    if not target.exists():
        return default
    with target.open(encoding="utf-8") as f:
        return json.load(f)


def _load_glob(path: Path, pattern: str) -> list:
    """Concatenate every JSON array matching a pattern (files come paginated)."""
    out: list = []
    for target in sorted(path.glob(pattern)):
        with target.open(encoding="utf-8") as f:
            chunk = json.load(f)
        if isinstance(chunk, list):
            out.extend(chunk)
    return out


def _legacy_payload(profile: dict, settings: dict) -> dict:
    account = settings.get("account") or {}
    browsing = settings.get("browsing") or {}
    ids = profile.get("ids") or {}
    images = profile.get("images") or {}
    return {
        "schema_version": 1,
        "profile": {
            "username": profile.get("username"),
            "name": profile.get("name"),
            "trakt_id": ids.get("trakt"),
            "slug": ids.get("slug"),
            "joined_at": profile.get("joined_at"),
            "location": profile.get("location"),
            "about": profile.get("about"),
            "avatar_url": ((images.get("avatar") or {}).get("full")),
            "private": profile.get("private"),
            "vip": profile.get("vip"),
        },
        "settings": {
            "timezone": account.get("timezone"),
            "date_format": account.get("date_format"),
            "time_24hr": account.get("time_24hr"),
            "locale": browsing.get("locale"),
            "theme": browsing.get("app_theme"),
            "favorite_genres": ((browsing.get("genres") or {}).get("favorites") or []),
            "calendar": browsing.get("calendar") or {},
            "progress": browsing.get("progress") or {},
        },
    }


def extract_export(source: str | Path, dest: str | Path) -> Path:
    """Return a folder of export JSON, unzipping first when given a ZIP.

    Trakt's ZIP may nest everything under a single top-level directory; return
    the directory that actually holds the JSON files.
    """
    source = Path(source)
    if source.is_dir():
        root = source
    else:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as zf:
            for member in zf.namelist():
                # Reject absolute paths and traversal in untrusted archives.
                target = (dest / member).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise ValueError(f"unsafe path in archive: {member}")
            zf.extractall(dest)
        root = dest

    if not any(root.glob("*.json")):
        nested = [p for p in root.iterdir() if p.is_dir() and any(p.glob("*.json"))]
        if nested:
            return nested[0]
    return root


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for target in sorted(path.glob("*.json")):
        digest.update(target.name.encode())
        digest.update(target.read_bytes())
    return digest.hexdigest()


def run_offline_import(user, folder: str | Path, *, job: ImportJob | None = None) -> ImportJob:
    """Import history, ratings, watchlist and lists from an export folder."""
    from . import trakt

    path = Path(folder)
    fingerprint = _fingerprint(path)
    if job is None:
        job, _ = ImportJob.objects.get_or_create(
            user=user, kind="trakt", archive_fingerprint=fingerprint,
            defaults={"phase": "queued"},
        )
        if job.state == "done":
            return job
    job.state = "running"
    job.phase = "preprocessing"
    job.error = ""
    job.processed = 0
    job.total = 0
    job.needs_review = []
    job.finished_at = None
    job.started_at = timezone.now()
    job.save(update_fields=[
        "state", "phase", "error", "processed", "total", "needs_review",
        "finished_at", "started_at",
    ])

    try:
        history = _load_glob(path, "watched-history-*.json")
        ratings = _load_glob(path, "ratings-*.json")
        watchlist = _load(path, "lists-watchlist.json", [])

        list_items: list[tuple[dict, list]] = []
        for meta in _load(path, "lists-lists.json", []):
            ids = meta.get("ids") or {}
            # Files are named lists-list-<trakt_id>-<slug>.json; match on the
            # stable numeric id and fall back to the slug.
            items: list = []
            for key in (ids.get("trakt"), ids.get("slug")):
                if key is None:
                    continue
                pattern = (
                    f"lists-list-{key}-*.json" if isinstance(key, int)
                    else f"lists-list-*-{key}.json"
                )
                items = _load_glob(path, pattern)
                if items:
                    break
            if items:
                list_items.append((meta, items))
        favorites = _load(path, "lists-favorites.json", [])
        if favorites:
            list_items.append(({"name": "Favorites"}, favorites))

        if not (history or ratings or watchlist or list_items):
            raise ValueError(f"No Trakt export JSON found in {path}")

        return trakt.process_datasets(
            user, job,
            history=history, ratings=ratings,
            watchlist=watchlist, list_items=list_items,
        )
    except Exception as exc:  # noqa: BLE001 — never strand the job in `running`
        job.refresh_from_db()
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"[:2000]
        job.finished_at = timezone.now()
        job.save()
        return job


def import_legacy_and_hidden(user, folder: str | Path) -> dict:
    """Add archival/profile and visibility data; absence never clears local flags."""
    path = Path(folder)
    profile = _load(path, "user-profile.json", {})
    settings = _load(path, "user-settings.json", {})
    if settings.get("user") and not profile:
        profile = settings["user"]
    legacy, _ = TraktLegacy.objects.update_or_create(
        user=user, defaults={"metadata": _legacy_payload(profile, settings)}
    )

    hidden_by_tmdb: dict[str, dict] = {}
    for filename, field in (
        ("hidden-progress-watched.json", "up_next"),
        ("hidden-calendar.json", "releases"),
    ):
        for entry in _load(path, filename, []):
            tmdb_id = (((entry.get("show") or {}).get("ids") or {}).get("tmdb"))
            if not tmdb_id:
                continue
            hidden_by_tmdb.setdefault(str(tmdb_id), {})[field] = parse_datetime(
                entry.get("hidden_at") or ""
            )

    provider = registry.get("tmdb")
    imported = 0
    errors = []
    for tmdb_id, flags in hidden_by_tmdb.items():
        try:
            show = services.upsert_item(provider.details(MediaType.TV.value, tmdb_id))
            services.set_show_visibility(
                user=user,
                show=show,
                up_next=True if "up_next" in flags else None,
                releases=True if "releases" in flags else None,
                hidden_at=flags.get("up_next") or flags.get("releases"),
            )
            # Preserve distinct Trakt timestamps when both sections overlap.
            pref = show.feed_preferences.get(user=user)
            if flags.get("up_next"):
                pref.up_next_hidden_at = flags["up_next"]
            if flags.get("releases"):
                pref.releases_hidden_at = flags["releases"]
            pref.save()
            imported += 1
        except Exception as exc:  # per-row best effort; surfaced to the user
            errors.append({"tmdb_id": tmdb_id, "error": str(exc)})
    return {"legacy_id": legacy.pk, "hidden_shows": imported, "errors": errors}
