"""HTMX endpoints for the imports/exports settings page."""

from __future__ import annotations

import io
import secrets
import zipfile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from api.models import APIToken
from core import messages as user_messages
from core.models import AppSettings
from core.settings_store import (
    DEFAULT_SOURCE_ADDON_URL,
    qbittorrent_config,
    tmdb_api_key,
)
from providers import registry
from providers.base import ProviderError
from providers.tmdb import TMDBProvider

from . import csv_books, csv_movies_shows
from .files import csv_export_response, decode_csv_upload
from .models import ImportJob, TraktLegacy


def _toast_for_job(user, job: ImportJob) -> None:
    """One toast per import: success summary or a failure pointer."""
    if job.state == "failed":
        user_messages.error(user, f"Import failed: {job.error[:200]}")
        return
    review_n = job.pending_review_count
    if review_n:
        user_messages.warning(
            user,
            f"Imported {job.processed}/{job.total}. "
            f"{review_n} row{'s' if review_n != 1 else ''} need review.",
        )
    else:
        user_messages.success(user, f"Imported {job.processed} rows.")


@login_required
def index(request):
    ImportJob.fail_stale_running(request.user)
    jobs = ImportJob.objects.filter(user=request.user)[:20]
    key_configured = bool(tmdb_api_key())
    qbit = qbittorrent_config()
    stored = AppSettings.objects.filter(singleton=True).first()
    return render(
        request,
        "imports/index.html",
        {
            "jobs": jobs,
            "tmdb_key_valid": key_configured,
            "tmdb_status": "configured" if key_configured else "not configured",
            "api_token": APIToken.objects.filter(user=request.user).first(),
            "source_addon_url": stored.source_addon_url if stored else "",
            "default_addon_url": DEFAULT_SOURCE_ADDON_URL,
            "qbit": {**qbit, "password_set": bool(qbit["password"])},
        },
    )


@login_required
@require_http_methods(["POST"])
def save_sources_settings(request):
    """Persist Source lookup + download client settings (neutral labels).

    Mirrors the TMDB key flow: validate before saving so a typo doesn't
    silently break every lookup later. Leaving the addon URL blank resets it
    to the default provider.
    """
    addon_url = request.POST.get("source_addon_url", "").strip().rstrip("/")
    host = request.POST.get("qbittorrent_host", "").strip()
    port_raw = request.POST.get("qbittorrent_port", "").strip()
    username = request.POST.get("qbittorrent_username", "").strip()
    password = request.POST.get("qbittorrent_password", "")

    app_settings = AppSettings.load()

    if addon_url:
        import httpx

        try:
            resp = httpx.get(
                f"{addon_url}/addon/catalog/series/tt0844371.json",
                timeout=10.0,
            )
            # Any HTTP response means a live addon lives at that URL; content
            # shape differences (or 404s on the probe path) are fine.
            if resp.status_code >= 500:
                raise httpx.HTTPError(f"status {resp.status_code}")
        except httpx.HTTPError:
            user_messages.error(request.user, "That addon URL didn't respond — not saved.")
            return redirect("imports:index")

    port = None
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            user_messages.error(request.user, "Client port must be a number.")
            return redirect("imports:index")
        if not 1 <= port <= 65535:
            user_messages.error(request.user, "Client port must be 1–65535.")
            return redirect("imports:index")

    app_settings.source_addon_url = addon_url  # blank → default at read time
    app_settings.qbittorrent_host = host
    app_settings.qbittorrent_port = port
    app_settings.qbittorrent_username = username
    if password:  # blank input keeps the stored password
        app_settings.set_secret("qbittorrent_password", password)
    app_settings.save()

    # Light validation of the client, if one was given.
    if host:
        from sources.download_client import DownloadClient

        if not DownloadClient().ping():
            user_messages.warning(
                request.user,
                "Saved, but the download client didn't respond — check host/port.",
            )
        else:
            user_messages.success(request.user, "Source settings saved.")
    else:
        user_messages.success(request.user, "Source settings saved.")
    return redirect("imports:index")


@login_required
@require_http_methods(["POST"])
def save_tmdb_key(request):
    value = request.POST.get("tmdb_api_key", "").strip()
    if not value:
        user_messages.error(request.user, "Enter a TMDB API key.")
        return redirect("imports:index")

    provider = TMDBProvider(api_key=value)
    try:
        provider._http.get_json("/configuration")
    except ProviderError:
        user_messages.error(request.user, "TMDB rejected that API key.")
        return redirect("imports:index")
    finally:
        provider._http.close()

    app_settings = AppSettings.load()
    app_settings.set_tmdb_api_key(value)
    app_settings.save()
    registry.reset_cache()
    user_messages.success(request.user, "TMDB API key saved.")
    return redirect("imports:index")


@login_required
def import_jobs(request):
    ImportJob.fail_stale_running(request.user)
    jobs = ImportJob.objects.filter(user=request.user)[:20]
    return render(request, "partials/_import_jobs.html", {"jobs": jobs})


def _upload_csv(request, importer):
    upload = request.FILES.get("file")
    if not upload:
        return HttpResponseBadRequest("file required")
    text = decode_csv_upload(upload)
    if text is None:
        return HttpResponseBadRequest("file must be UTF-8 encoded CSV")
    job = importer(request.user, io.StringIO(text))
    _toast_for_job(request.user, job)
    return redirect(reverse("imports:job", args=[job.pk]))


@login_required
@require_http_methods(["POST"])
def upload_csv_movies_shows(request):
    return _upload_csv(request, csv_movies_shows.import_csv)


@login_required
@require_http_methods(["POST"])
def upload_csv_books(request):
    return _upload_csv(request, csv_books.import_csv)


@login_required
@require_http_methods(["POST"])
def upload_trakt_export(request):
    """Import an uploaded Trakt export ZIP (or the folder under data/)."""
    import tempfile
    from pathlib import Path

    from .trakt_export import extract_export, import_legacy_and_hidden, run_offline_import

    upload = request.FILES.get("file")
    with tempfile.TemporaryDirectory() as tmp:
        if upload:
            archive = Path(tmp) / "trakt-export.zip"
            with archive.open("wb") as f:
                for chunk in upload.chunks():
                    f.write(chunk)
            try:
                folder = extract_export(archive, Path(tmp) / "extracted")
            except (zipfile.BadZipFile, ValueError) as exc:
                return HttpResponseBadRequest(f"Invalid Trakt export ZIP: {exc}")
        else:
            folder = Path(settings.BASE_DIR).parent / "data" / "trakt-export"
            if not folder.is_dir():
                return HttpResponseBadRequest(
                    "Upload a Trakt export ZIP, or place one in data/trakt-export/."
                )

        job = run_offline_import(request.user, folder)
        # Profile + hidden preferences ride along with the same export.
        try:
            import_legacy_and_hidden(request.user, folder)
        except Exception as exc:  # noqa: BLE001 — archival extras are best effort
            user_messages.warning(request.user, f"Legacy/hidden import skipped: {exc}")

    _toast_for_job(request.user, job)
    return redirect(reverse("imports:job", args=[job.pk]))


@login_required
def trakt_legacy(request):
    legacy = TraktLegacy.objects.filter(user=request.user).first()
    return render(request, "imports/trakt_legacy.html", {"legacy": legacy})


def _review_rows(job: ImportJob) -> list[dict]:
    rows = []
    for index, entry in enumerate(job.needs_review):
        row = entry.get("row") or {}
        rows.append(
            {
                "index": index,
                "entry": entry,
                "title": row.get("Title") or "Untitled row",
                "author": row.get("Authors") or row.get("Author") or "Unknown author",
                "isbn": row.get("ISBN/UID") or row.get("ISBN13") or row.get("ISBN") or "",
            }
        )
    return rows


@login_required
def job_detail(request, job_id: int):
    job = get_object_or_404(ImportJob, pk=job_id, user=request.user)
    return render(request, "imports/job.html", {"job": job, "review_rows": _review_rows(job)})


@login_required
def job_progress(request, job_id: int):
    job = get_object_or_404(ImportJob, pk=job_id, user=request.user)
    return render(request, "imports/_job_progress.html", {"job": job})


def _book_review_entry(request, job_id: int, row_index: int):
    job = get_object_or_404(ImportJob, pk=job_id, user=request.user, kind="csv_books", state="done")
    if row_index < 0 or row_index >= len(job.needs_review):
        return job, None
    return job, job.needs_review[row_index]


def _review_query(entry: dict) -> str:
    row = entry.get("row") or {}
    return row.get("Title") or row.get("ISBN/UID") or row.get("ISBN13") or row.get("ISBN") or ""


@login_required
def review_book_search(request, job_id: int, row_index: int):
    job, entry = _book_review_entry(request, job_id, row_index)
    if entry is None:
        return HttpResponseBadRequest("invalid review row")
    if entry.get("resolution"):
        return HttpResponseBadRequest("review row already resolved")
    query = request.GET.get("q", "").strip() or _review_query(entry)
    try:
        results = registry.get("openlibrary").search("book", query).hits if query else []
    except ProviderError as exc:
        return render(
            request,
            "imports/_book_review_results.html",
            {
                "job": job,
                "row_index": row_index,
                "query": query,
                "results": [],
                "search_error": str(exc),
            },
        )
    result_rows = []
    for hit in results:
        token = secrets.token_urlsafe(18)
        request.session[f"book_review:{job.pk}:{row_index}:{hit.media_id}"] = token
        result_rows.append({"hit": hit, "selection_token": token})
    return render(
        request,
        "imports/_book_review_results.html",
        {
            "job": job,
            "row_index": row_index,
            "query": query,
            "results": result_rows,
        },
    )


@login_required
@require_http_methods(["POST"])
def review_book_match(request, job_id: int, row_index: int):
    from providers.base import SearchHit

    job, entry = _book_review_entry(request, job_id, row_index)
    if entry is None:
        return HttpResponseBadRequest("invalid review row")
    if entry.get("resolution"):
        return HttpResponseBadRequest("review row already resolved")
    olid = request.POST.get("olid", "").strip()
    if not olid:
        return HttpResponseBadRequest("Open Library work required")
    title = request.POST.get("title", "").strip()
    if not title:
        return HttpResponseBadRequest("Open Library title required")
    expected_token = request.session.pop(f"book_review:{job.pk}:{row_index}:{olid}", None)
    if expected_token != request.POST.get("selection_token"):
        return HttpResponseBadRequest("search result expired; search again")
    try:
        year = int(request.POST["year"]) if request.POST.get("year") else None
        pages = int(request.POST["pages"]) if request.POST.get("pages") else None
    except ValueError:
        return HttpResponseBadRequest("invalid Open Library metadata")
    hit = SearchHit(
        source="openlibrary",
        media_type="book",
        media_id=olid,
        title=title,
        image=request.POST.get("image", ""),
        year=year,
        runtime=pages,
        raw={"isbn": request.POST.getlist("isbn")},
    )
    tracking = csv_books.apply_match(request.user, entry, hit)
    review = list(job.needs_review)
    review[row_index] = {
        **entry,
        "resolution": "matched",
        "matched_olid": olid,
        "matched_item_id": tracking.item_id,
    }
    job.needs_review = review
    job.save(update_fields=["needs_review"])
    user_messages.success(request.user, f"Matched {tracking.item.title}.")
    return redirect("imports:job", job_id=job.pk)


@login_required
@require_http_methods(["POST"])
def review_book_skip(request, job_id: int, row_index: int):
    job, entry = _book_review_entry(request, job_id, row_index)
    if entry is None:
        return HttpResponseBadRequest("invalid review row")
    if entry.get("resolution"):
        return HttpResponseBadRequest("review row already resolved")
    review = list(job.needs_review)
    review[row_index] = {**entry, "resolution": "skipped"}
    job.needs_review = review
    job.save(update_fields=["needs_review"])
    user_messages.success(request.user, "Book row skipped.")
    return redirect("imports:job", job_id=job.pk)


# -------------------------------------------------------------------- exports


@login_required
def export_movies_shows(request):
    return csv_export_response(
        request.user,
        csv_movies_shows.export_csv,
        "intake-movies-shows.csv",
    )


@login_required
def export_books(request):
    return csv_export_response(request.user, csv_books.export_csv, "intake-books.csv")


@login_required
def export_json(request):
    """Versioned JSON archive including simple-history snapshots."""
    from .archive import export_payload

    return JsonResponse(export_payload(request.user), json_dumps_params={"indent": 2})
