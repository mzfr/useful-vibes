from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from core.enums import MediaType, Status
from core.models import Item
from providers import registry
from providers.base import ProviderError

from . import services
from .models import Book

STATUS_FILTERS = (
    ("all", "All", None),
    ("reading", "Reading", Status.IN_PROGRESS.value),
    ("to_read", "To Read", Status.PLANNING.value),
    ("read", "Read", Status.COMPLETED.value),
    ("dnf", "DNF", Status.DROPPED.value),
)
STATUS_CHOICES = (
    (Status.IN_PROGRESS.value, "Reading"),
    (Status.PLANNING.value, "To Read"),
    (Status.COMPLETED.value, "Read"),
    (Status.DROPPED.value, "DNF"),
)
STATUS_SLUGS = {status: slug for slug, _label, status in STATUS_FILTERS if status}
SORT_OPTIONS = (
    ("added_desc", "Recently added"),
    ("added_asc", "First added"),
    ("title_asc", "Title A–Z"),
    ("title_desc", "Title Z–A"),
    ("started_desc", "Recently started"),
    ("finished_desc", "Recently finished"),
    ("finished_asc", "First finished"),
    ("score_desc", "Highest rated"),
    ("published_desc", "Newest published"),
    ("published_asc", "Oldest published"),
)
SORT_VALUES = {value for value, _label in SORT_OPTIONS}


@dataclass
class BookRow:
    tracking: Book
    title: str
    author: str

    @property
    def item(self) -> Item:
        return self.tracking.item

    @property
    def cover_url(self) -> str:
        return self.item.image

    @property
    def pages(self) -> int | None:
        return self.item.max_progress

    @property
    def progress(self) -> int:
        return self.tracking.progress

    @property
    def score(self) -> int | None:
        return self.tracking.score

    @property
    def status(self) -> str:
        return STATUS_SLUGS.get(self.tracking.status, self.tracking.status)

    @property
    def status_label(self) -> str:
        return dict(STATUS_CHOICES).get(self.tracking.status, self.tracking.get_status_display())

    @property
    def date_label(self) -> str:
        value = self.tracking.end_date or self.tracking.start_date or self.tracking.created_at.date()
        return value.strftime("%-d %b %Y")


def _title_author(item: Item) -> tuple[str, str]:
    raw = item.raw or {}
    search = raw.get("search") or {}
    title = search.get("title") or item.title
    authors = search.get("author_name") or []
    if authors:
        return title, authors[0]
    title, separator, author = item.title.partition(" — ")
    return title, author if separator else ""


def _book_rows(queryset) -> list[BookRow]:
    rows = []
    for tracking in queryset.select_related("item"):
        title, author = _title_author(tracking.item)
        rows.append(BookRow(tracking=tracking, title=title, author=author))
    return rows


def _counts(user) -> dict[str, int]:
    counts = {slug: 0 for slug, _label, _status in STATUS_FILTERS}
    counts["all"] = Book.objects.filter(user=user).count()
    for _slug, _label, status in STATUS_FILTERS:
        if status:
            counts[STATUS_SLUGS[status]] = Book.objects.filter(user=user, status=status).count()
    return counts


def _selected_filter(user, requested: str) -> str:
    valid = {slug for slug, _label, _status in STATUS_FILTERS}
    if requested in valid:
        return requested
    if Book.objects.filter(user=user, status=Status.IN_PROGRESS.value).exists():
        return "reading"
    return "to_read"


def _selected_sort(requested: str) -> str:
    return requested if requested in SORT_VALUES else "added_desc"


def _ordered_books(queryset, selected_sort: str):
    ordering = {
        "added_desc": ("-created_at", "item__title"),
        "added_asc": ("created_at", "item__title"),
        "title_asc": ("item__title",),
        "title_desc": ("-item__title",),
        "started_desc": (F("start_date").desc(nulls_last=True), "item__title"),
        "finished_desc": (F("end_date").desc(nulls_last=True), "item__title"),
        "finished_asc": (F("end_date").asc(nulls_last=True), "item__title"),
        "score_desc": (F("score").desc(nulls_last=True), "item__title"),
        "published_desc": (F("item__release_date").desc(nulls_last=True), "item__title"),
        "published_asc": (F("item__release_date").asc(nulls_last=True), "item__title"),
    }
    return queryset.order_by(*ordering[selected_sort])


def _table_context(user, selected: str, selected_sort: str = "added_desc") -> dict:
    selected = _selected_filter(user, selected)
    selected_sort = _selected_sort(selected_sort)
    status = next(status for slug, _label, status in STATUS_FILTERS if slug == selected)
    queryset = Book.objects.filter(user=user)
    if status:
        queryset = queryset.filter(status=status)
    return {
        "books": _book_rows(_ordered_books(queryset, selected_sort)),
        "selected_filter": selected,
        "selected_sort": selected_sort,
        "sort_options": SORT_OPTIONS,
        "status_choices": STATUS_FILTERS[1:],
    }


@login_required
def books_index(request):
    selected = _selected_filter(request.user, request.GET.get("status", ""))
    selected_sort = _selected_sort(request.GET.get("sort", ""))
    counts = _counts(request.user)
    filters = [
        {"value": slug, "label": label, "count": counts[slug], "active": slug == selected}
        for slug, label, _status in STATUS_FILTERS
    ]
    context = {
        **_table_context(request.user, selected, selected_sort),
        "filters": filters,
        "total_count": counts["all"],
        "reading_count": counts["reading"],
    }
    template = "tracking/books/_books_table.html" if request.headers.get("HX-Request") else "tracking/books/index.html"
    return render(request, template, context)


@login_required
def book_search(request):
    query = request.GET.get("q", "").strip()
    results = []
    error = ""
    if len(query) >= 2:
        try:
            results = registry.get("openlibrary").search(MediaType.BOOK.value, query).hits[:8]
        except ProviderError as exc:
            error = str(exc)
    return render(
        request,
        "tracking/books/_search_results.html",
        {"query": query, "results": results, "error": error},
    )


@login_required
@require_http_methods(["POST"])
def book_add(request):
    olid = request.POST.get("olid", "").strip()
    if not olid:
        return HttpResponseBadRequest("missing Open Library ID")
    try:
        item, _metadata = services.resolve_root_item(
            "openlibrary", MediaType.BOOK.value, olid,
        )
    except ProviderError as exc:
        return HttpResponseBadRequest(str(exc))
    services.update_tracking(
        user=request.user,
        item=item,
        status=Status.PLANNING.value,
        bump_progressed_at=False,
    )
    return render(
        request,
        "tracking/books/_search_added.html",
        {"item": item},
    )


def _book_item(olid: str) -> Item:
    return get_object_or_404(
        Item,
        source="openlibrary",
        media_type=MediaType.BOOK.value,
        media_id=olid,
    )


def _reading_sessions(tracking: Book) -> list[dict]:
    sessions = []
    previous = 0
    history = Book.history.filter(id=tracking.pk).order_by("history_date", "history_id")
    for snapshot in history:
        if snapshot.progress > previous:
            sessions.append({
                "date_label": snapshot.history_date.strftime("%-d %b %Y"),
                "from_page": previous,
                "to_page": snapshot.progress,
            })
        previous = snapshot.progress
    return list(reversed(sessions))


@login_required
def book_detail(request, olid: str):
    item = _book_item(olid)
    tracking = Book.objects.filter(user=request.user, item=item).first()
    title, author = _title_author(item)
    raw = item.raw or {}
    search = raw.get("search") or {}
    percent = 0
    if tracking and item.max_progress:
        percent = min(100, round(tracking.progress / item.max_progress * 100))
    return render(request, "tracking/books/detail.html", {
        "book": {
            "item": item,
            "olid": item.media_id,
            "title": title,
            "author": author,
            "cover_url": item.image,
            "year": item.release_date.year if item.release_date else None,
            "pages": item.max_progress,
            "genre": (search.get("subject") or [""])[0],
            "description": item.synopsis,
        },
        "tracking": tracking,
        "percent": percent,
        "pace_label": f"{tracking.progress} pages" if tracking else "Not started",
        "status_choices": STATUS_CHOICES,
        "score_range": range(1, 11),
        "sessions": _reading_sessions(tracking) if tracking else [],
    })


def _posted_date(request, field: str):
    raw = request.POST.get(field, "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc


def _safe_next(request) -> str | None:
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return target
    return None


def _redirect_or_table(request, selected: str):
    selected_sort = _selected_sort(request.POST.get("sort", ""))
    if request.headers.get("HX-Request"):
        context = _table_context(request.user, selected, selected_sort)
        counts = _counts(request.user)
        context["filters"] = [
            {"value": slug, "label": label, "count": counts[slug], "active": slug == selected}
            for slug, label, _status in STATUS_FILTERS
        ]
        return render(request, "tracking/books/_books_table.html", context)
    return redirect(
        _safe_next(request)
        or f"{reverse('tracking:books')}?status={selected}&sort={selected_sort}"
    )


@login_required
@require_http_methods(["POST"])
def book_status(request, item_id: int):
    item = get_object_or_404(Item, pk=item_id, media_type=MediaType.BOOK.value)
    status = request.POST.get("status", "")
    valid = {value for value, _label in STATUS_CHOICES}
    if status not in valid:
        return HttpResponseBadRequest("invalid book status")
    row, _created = services.get_or_create_tracking(user=request.user, item=item)
    if status == Status.PLANNING.value:
        row.progress = 0
        row.start_date = None
        row.end_date = None
    elif status == Status.IN_PROGRESS.value:
        if item.max_progress and row.progress >= item.max_progress:
            row.progress = 0
        row.start_date = row.start_date or timezone.localdate()
        row.end_date = None
    elif status == Status.COMPLETED.value:
        row.start_date = row.start_date or timezone.localdate()
        row.end_date = row.end_date or timezone.localdate()
        if item.max_progress:
            row.progress = item.max_progress
    elif status == Status.DROPPED.value:
        row.start_date = row.start_date or timezone.localdate()
        row.end_date = None
    row.status = status
    row.progressed_at = timezone.now()
    row.save()
    return _redirect_or_table(request, request.POST.get("filter", "all"))


@login_required
@require_http_methods(["POST"])
def book_dates(request, item_id: int):
    item = get_object_or_404(Item, pk=item_id, media_type=MediaType.BOOK.value)
    row = get_object_or_404(Book, user=request.user, item=item)
    try:
        start_date = _posted_date(request, "start_date")
        end_date = _posted_date(request, "end_date")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if start_date and end_date and end_date < start_date:
        return HttpResponseBadRequest("finish date cannot be before start date")
    row.start_date = start_date
    row.end_date = end_date
    row.save(update_fields=["start_date", "end_date", "updated_at"])
    return redirect(_safe_next(request) or item.get_absolute_url())


@login_required
@require_http_methods(["POST"])
def book_progress(request, item_id: int):
    item = get_object_or_404(Item, pk=item_id, media_type=MediaType.BOOK.value)
    unit = request.POST.get("progress_unit", "pages")
    try:
        value = int(request.POST.get("progress", ""))
    except ValueError:
        return HttpResponseBadRequest("invalid progress")
    if unit == "pages":
        if value < 0:
            return HttpResponseBadRequest("invalid progress")
        progress = value
    elif unit == "percent":
        if value < 0 or value > 100:
            return HttpResponseBadRequest("percentage must be between 0 and 100")
        if not item.max_progress:
            return HttpResponseBadRequest("percentage requires a known page count")
        progress = (value * item.max_progress + 50) // 100
    else:
        return HttpResponseBadRequest("invalid progress unit")
    row, _created = services.get_or_create_tracking(user=request.user, item=item)
    row.progress = progress
    row.status = Status.IN_PROGRESS.value
    row.start_date = row.start_date or timezone.localdate()
    row.end_date = None
    row.progressed_at = timezone.now()
    row.save()
    return redirect(_safe_next(request) or item.get_absolute_url())


@login_required
@require_http_methods(["POST"])
def book_rate(request, item_id: int):
    item = get_object_or_404(Item, pk=item_id, media_type=MediaType.BOOK.value)
    try:
        score = int(request.POST.get("score", ""))
    except ValueError:
        return HttpResponseBadRequest("invalid score")
    if score not in range(1, 11):
        return HttpResponseBadRequest("score must be between 1 and 10")
    services.update_tracking(user=request.user, item=item, score=score, bump_progressed_at=False)
    return redirect(_safe_next(request) or item.get_absolute_url())
