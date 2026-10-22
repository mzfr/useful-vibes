"""Top-level django-ninja API."""
from __future__ import annotations

import io
import zipfile
from datetime import UTC

from django.shortcuts import get_object_or_404
from ninja import File, NinjaAPI, UploadedFile
from ninja.security import django_auth

from core.enums import MediaType, Status
from core.models import Item
from imports import csv_books, csv_movies_shows
from imports.files import csv_export_response, decode_csv_upload
from imports.models import ImportJob
from providers import registry
from providers.base import NotSupported, ProviderError
from stats.services import build_stats
from tracking import services
from tracking.models import MODEL_FOR_MEDIA_TYPE, CompletionEvent, Season
from tracking.up_next import in_progress_books, in_progress_movies, shows_for_user

from . import schemas
from .auth import TokenAuth
from .models import APIToken

# TokenAuth MUST be first: django_auth (APIKeyCookie, csrf=True) *raises* 403
# on any POST without a CSRF token, and ninja stops at the first authenticator
# that raises — so bearer-token writes would never work if it ran first.
# TokenAuth returns None when there's no Authorization header, falling through
# to session auth cleanly.
api = NinjaAPI(
    title="Intake API",
    version="1",
    urls_namespace="api",
    auth=[TokenAuth(), django_auth],
    docs_url="/docs",
)


# ----------------------------------------------------------------- search/items


@api.get("/search", response=schemas.SearchOut, tags=["search"])
def search(request, q: str, type: str = "movie", source: str | None = None):
    """Unified search. `source` defaults to the canonical provider for the type."""
    try:
        provider = registry.get(source) if source else registry.for_media_type(type)
        results = provider.search(type, q)
    except (NotSupported, ProviderError) as exc:
        return api.create_response(request, {"error": str(exc)}, status=400)
    return {
        "hits": [
            {
                "source": h.source, "media_type": h.media_type, "media_id": h.media_id,
                "title": h.title, "image": h.image, "year": h.year, "synopsis": h.synopsis,
            } for h in results.hits
        ],
        "page": results.page,
        "total_pages": results.total_pages,
        "total_results": results.total_results,
    }


@api.get("/items/{source}/{media_type}/{media_id}", response=schemas.MetadataOut, tags=["items"])
def item_details(request, source: str, media_type: str, media_id: str):
    """Local-first: reads Item from DB; only calls provider if missing."""
    try:
        item, _metadata = services.resolve_root_item(source, media_type, media_id)
    except (NotSupported, ProviderError) as exc:
        return api.create_response(request, {"error": str(exc)}, status=400)
    return {
        "source": item.source, "media_type": item.media_type, "media_id": item.media_id,
        "title": item.title, "image": item.image, "synopsis": item.synopsis,
        "genres": (item.raw or {}).get("genres", []),
        "release_date": item.release_date, "runtime": item.runtime,
        "max_progress": item.max_progress, "external_ids": {},
    }


# ------------------------------------------------------------------ tracking


def _resolve_item_for_tracking(payload: schemas.TrackIn, media_type: str) -> Item:
    """Local-first item resolution. Only calls provider if Item is missing."""
    if media_type in (MediaType.MOVIE.value, MediaType.TV.value, MediaType.BOOK.value):
        item, _metadata = services.resolve_root_item(
            payload.source, media_type, payload.media_id,
        )
        return item
    if media_type == MediaType.SEASON.value:
        if payload.season_number is None:
            raise ValueError("season_number required")
        item = Item.objects.filter(
            source=payload.source, media_type=MediaType.SEASON.value,
            media_id=str(payload.media_id), season_number=payload.season_number,
        ).first()
        if item is not None and item.metadata_complete:
            return item
        provider = registry.get(payload.source)
        return services.materialize_season(
            provider, payload.media_id, payload.season_number,
        )
    if media_type == MediaType.EPISODE.value:
        if payload.season_number is None or payload.episode_number is None:
            raise ValueError("season_number + episode_number required")
        item = Item.objects.filter(
            source=payload.source, media_type=MediaType.EPISODE.value,
            media_id=str(payload.media_id),
            season_number=payload.season_number,
            episode_number=payload.episode_number,
        ).first()
        if item is not None and item.metadata_complete:
            return item
        provider = registry.get(payload.source)
        season_item = services.materialize_season(
            provider, payload.media_id, payload.season_number,
        )
        ep = Item.objects.filter(
            parent=season_item, media_type=MediaType.EPISODE.value,
            episode_number=payload.episode_number,
        ).first()
        if ep is None:
            raise ValueError("episode not found in season payload")
        return ep
    raise ValueError(f"unsupported media_type {media_type!r}")


def _tracking_to_out(row, media_type: str) -> dict:
    return {
        "id": row.pk,
        "media_type": media_type,
        "item_id": row.item_id,
        "status": row.status,
        "score": row.score,
        "progress": row.progress,
        "repeats": row.repeats,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "progressed_at": row.progressed_at,
    }


@api.post("/tracking/{media_type}", response=schemas.TrackOut, tags=["tracking"])
def tracking_upsert(request, media_type: str, payload: schemas.TrackIn):
    try:
        item = _resolve_item_for_tracking(payload, media_type)
    except (ValueError, NotSupported, ProviderError) as exc:
        return api.create_response(request, {"error": str(exc)}, status=400)
    row = services.update_tracking(
        user=request.user, item=item,
        score=payload.score, progress=payload.progress, status=payload.status,
        start_date=payload.start_date, end_date=payload.end_date,
    )
    return _tracking_to_out(row, media_type)


@api.post("/tracking/{media_type}/{tracking_id}/progress", response=schemas.TrackOut, tags=["tracking"])
def tracking_progress(request, media_type: str, tracking_id: int, payload: schemas.ProgressIn):
    model = MODEL_FOR_MEDIA_TYPE.get(media_type)
    if model is None:
        return api.create_response(request, {"error": "bad media_type"}, status=400)
    row = get_object_or_404(model, pk=tracking_id, user=request.user)
    # Route through the service so progressed_at is bumped — Up Next staleness
    # ordering depends on it.
    row = services.update_tracking(
        user=request.user, item=row.item, progress=payload.progress
    )
    return _tracking_to_out(row, media_type)


@api.post("/tracking/{media_type}/{tracking_id}/rewatch", response=schemas.TrackOut, tags=["tracking"])
def tracking_rewatch(request, media_type: str, tracking_id: int):
    model = MODEL_FOR_MEDIA_TYPE.get(media_type)
    if model is None:
        return api.create_response(request, {"error": "bad media_type"}, status=400)
    row = get_object_or_404(
        model, pk=tracking_id, user=request.user, status=Status.COMPLETED.value
    )
    row.mark_watched_again()
    return _tracking_to_out(row, media_type)


@api.post("/tracking/season/{tracking_id}/bulk", response={200: dict}, tags=["tracking"])
def tracking_bulk_season(request, tracking_id: int, payload: schemas.BulkIn):
    """Bulk season ops. Shares `tracking.bulk.apply_bulk_action` with the HTMX view."""
    from tracking.bulk import BulkActionError, apply_bulk_action

    season_row = get_object_or_404(Season, pk=tracking_id, user=request.user)
    try:
        apply_bulk_action(
            user=request.user,
            season_item=season_row.item,
            action=payload.action,
            start=payload.start,
            end=payload.end,
        )
    except BulkActionError as exc:
        return api.create_response(request, {"error": str(exc)}, status=400)
    return {"ok": True}


# -------------------------------------------------------------- history


@api.post("/history", tags=["history"], auth=TokenAuth())
def history(request, payload: schemas.HistoryRangeIn):
    """Return movie and episode watches in the requested UTC time range."""
    if payload.from_.tzinfo is None or payload.to.tzinfo is None:
        return api.create_response(
            request, {"error": "from and to must include a timezone"}, status=400,
        )
    if payload.from_ >= payload.to:
        return api.create_response(
            request, {"error": "from must be earlier than to"}, status=400,
        )

    events = (
        CompletionEvent.objects.filter(
            user=request.user,
            item__media_type__in=[MediaType.MOVIE.value, MediaType.EPISODE.value],
            completed_at__gte=payload.from_,
            completed_at__lt=payload.to,
        )
        .select_related("item__parent__parent")
        .order_by("completed_at", "pk")
    )
    result = []
    for event in events:
        item = event.item
        title = item.title
        if item.media_type == MediaType.EPISODE.value:
            show = item.parent.parent if item.parent_id and item.parent.parent_id else None
            if show is not None:
                title = f"{show.title} — {title}"
        row = {
            "id": f"watch-{event.pk}",
            "watched_at": event.completed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "type": item.media_type,
            "title": title,
        }
        if item.runtime is not None:
            row["runtime_minutes"] = item.runtime
        result.append(row)
    return {"items": result}


# -------------------------------------------------------------- up-next / stats


@api.get("/me/up-next", tags=["me"])
def me_up_next(request):
    """Combined Up Next feed: shows, in-progress movies, in-progress books."""
    shows = [
        {
            "show_id": r.show_item.pk,
            "show_title": r.show_item.title,
            # A tracked show can have no unwatched episode yet (nothing
            # aired / nothing resolved) — the episode fields then stay null.
            "next_episode_id": r.next_episode.pk if r.next_episode else None,
            "next_episode_label": (
                f"S{r.next_episode.season_number:02d}E{r.next_episode.episode_number:02d} — "
                f"{r.next_episode.title}"
            )
            if r.next_episode
            else None,
            "air_date": r.next_episode.release_date if r.next_episode else None,
        }
        for r in shows_for_user(request.user)
    ]
    movies = [
        {
            "item_id": r.item.pk,
            "title": r.item.title,
            "status": r.tracking.status,
        }
        for r in in_progress_movies(request.user)
    ]
    books = [
        {
            "item_id": r.item.pk,
            "title": r.item.title,
            "progress": r.tracking.progress,
            "max_progress": r.item.max_progress,
        }
        for r in in_progress_books(request.user)
    ]
    return {"shows": shows, "movies": movies, "books": books}


@api.get("/me/stats", response=schemas.StatsOut, tags=["me"])
def me_stats(request, year: int | None = None):
    s = build_stats(request.user, year=year)
    return {
        "year": s.year,
        "movies_completed": s.totals.movies_completed,
        "shows_completed": s.totals.shows_completed,
        "episodes_watched": s.totals.episodes_watched,
        "books_finished": s.totals.books_finished,
        "minutes_watched": s.totals.minutes_watched,
        "pages_read": s.totals.pages_read,
        "top_genres": list(s.top_genres),
        "decades": list(s.decades),
    }


@api.get("/me/releases", tags=["me"])
def me_releases(request):
    from releases.services import build_feed
    feed = build_feed(request.user)
    return {
        "new_seasons": [
            {"show": s.show.title, "show_id": s.show.pk, "season_number": s.season_number,
             "air_date": s.air_date, "episode_count": s.episode_count}
            for s in feed["new_seasons"]
        ],
        "new_episodes": [
            {"show": e.show.title, "show_id": e.show.pk, "season_number": e.season_number,
             "episode_number": e.episode_number, "title": e.title, "air_date": e.air_date}
            for e in feed["new_episodes"]
        ],
    }


# ------------------------------------------------------------------- imports


def _job_to_out(job: ImportJob) -> dict:
    return {
        "id": job.pk, "kind": job.kind, "state": job.state,
        "processed": job.processed, "total": job.total, "percent": job.percent,
        "error": job.error, "needs_review_count": len(job.needs_review),
    }


@api.post("/imports/trakt", response=schemas.ImportJobOut, tags=["imports"])
def imports_trakt(request, file: UploadedFile = File(...)):
    """Import the official Trakt data-export ZIP (no OAuth/API token)."""
    import tempfile
    from pathlib import Path

    from imports.trakt_export import extract_export, run_offline_import

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "trakt-export.zip"
        archive.write_bytes(file.read())
        try:
            folder = extract_export(archive, Path(tmp) / "extracted")
        except (ValueError, zipfile.BadZipFile) as exc:
            return api.create_response(request, {"error": str(exc)}, status=400)
        job = run_offline_import(request.user, folder)
    return _job_to_out(job)


@api.post("/imports/csv", response=schemas.ImportJobOut, tags=["imports"])
def imports_csv(request, kind: str, file: UploadedFile = File(...)):
    text = decode_csv_upload(file)
    if text is None:
        return api.create_response(
            request, {"error": "file must be UTF-8 encoded CSV"}, status=400
        )
    if kind == "movies_shows":
        job = csv_movies_shows.import_csv(request.user, io.StringIO(text))
    elif kind == "books":
        job = csv_books.import_csv(request.user, io.StringIO(text))
    else:
        return api.create_response(request, {"error": "kind must be movies_shows or books"}, status=400)
    return _job_to_out(job)


@api.get("/imports/{job_id}", response=schemas.ImportJobOut, tags=["imports"])
def imports_job(request, job_id: int):
    job = get_object_or_404(ImportJob, pk=job_id, user=request.user)
    return _job_to_out(job)


# ------------------------------------------------------------------- exports


@api.get("/exports/csv", tags=["exports"])
def exports_csv(request, type: str):
    exports = {
        "movies_shows": (csv_movies_shows.export_csv, "intake-movies-shows.csv"),
        "books": (csv_books.export_csv, "intake-books.csv"),
    }
    try:
        exporter, filename = exports[type]
    except KeyError:
        return api.create_response(
            request, {"error": "type must be movies_shows or books"}, status=400,
        )
    return csv_export_response(request.user, exporter, filename)


@api.get("/me/export", tags=["me"])
def me_export(request):
    """Versioned JSON archive including simple-history snapshots."""
    from imports.archive import export_payload

    return export_payload(request.user)


# ---------------------------------------------------------- token management


@api.post("/me/token", response=schemas.TokenMintOut, tags=["me"], auth=django_auth)
def mint_token(request, payload: schemas.TokenMintIn):
    """Generate a new personal access token. Revokes the previous one.

    Session-only: a bearer token must not be able to mint its own replacement,
    otherwise a leaked token is self-perpetuating and expiry means nothing.
    """
    token, raw = APIToken.mint(request.user, ttl=payload.ttl, label=payload.label)
    return {"token": raw, "expires_at": token.expires_at}
