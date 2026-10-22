from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from .services import MEDIA_FILTERS, RANGES, build_stats


def _context(request, *, forced_year: int | None = None):
    range_key = "year" if forced_year is not None else request.GET.get("range", "all")
    media_key = request.GET.get("media", "screen")
    year = forced_year
    if year is None and range_key == "year" and request.GET.get("year"):
        try:
            year = int(request.GET["year"])
        except ValueError:
            year = None
        if year is not None and not 1900 <= year <= 2999:
            year = None

    stats = build_stats(
        request.user, range_key=range_key, year=year, media_key=media_key,
    )
    return {
        "stats": stats,
        "ranges": [{**r, "active": r["key"] == stats.range_key} for r in RANGES],
        "media_filters": [
            {**m, "active": m["key"] == stats.media_key} for m in MEDIA_FILTERS
        ],
        "selected_year": stats.chart_year,
        "current_year": timezone.localdate().year,
    }


@login_required
def index(request):
    return render(request, "stats/index.html", _context(request))


@login_required
def year(request, year: int):
    if year < 1900 or year > 2999:
        raise Http404()
    return render(request, "stats/index.html", _context(request, forced_year=year))
