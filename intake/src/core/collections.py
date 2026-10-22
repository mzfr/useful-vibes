"""The two built-in collections: /watchlist and /favorites.

Watchlist is a view over Status.PLANNING tracking rows; favourites are their
own table. Neither is a CustomList any more.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.enums import MediaType, Status
from core.models import Favorite, Item

FILTERS = [
    ("all", "All"),
    (MediaType.MOVIE.value, "Movies"),
    (MediaType.TV.value, "Shows"),
]

# The watchlist is the "saw a trailer, want to watch" surface for screen
# media. Books share Status.PLANNING ("To Read") with it, but they already
# have their own library view — mixing 100+ imported books in here would
# drown the movies and shows.
WATCHLIST_TYPES = [MediaType.MOVIE.value, MediaType.TV.value]


def _card(item: Item, subtitle: str = "") -> dict:
    return {
        "url": item.get_absolute_url(),
        "title": item.title,
        "image": item.image,
        "media_type": item.media_type,
        "year": item.release_date.year if item.release_date else None,
        "subtitle": subtitle,
    }


def watchlist_items(user, media_type: str = "all") -> list[Item]:
    """Planning movies and shows in the same order for local and shared views."""
    from tracking.models import MODEL_FOR_MEDIA_TYPE

    wanted = [media_type] if media_type in WATCHLIST_TYPES else WATCHLIST_TYPES
    rows = []
    for key in wanted:
        model = MODEL_FOR_MEDIA_TYPE[key]
        rows.extend(
            model.objects.filter(user=user, status=Status.PLANNING.value).select_related("item")
        )
    rows.sort(key=lambda r: r.progressed_at or r.created_at, reverse=True)
    return [row.item for row in rows]


@login_required
def watchlist(request):
    """Everything the user has marked as planning, newest intent first."""
    media_type = request.GET.get("type", "all")
    items = watchlist_items(request.user, media_type)

    return render(
        request,
        "core/watchlist.html",
        {
            "cards": [{**_card(item), "item_id": item.pk} for item in items],
            "removable": True,
            "media_type": media_type,
            "filters": FILTERS,
        },
    )


def _fully_watched_show_ids(user, shows: list[Item]) -> set[int]:
    """Shows with every known aired season materialized and episode watched."""
    from tracking.models import Episode

    show_ids = [show.pk for show in shows]
    if not show_ids:
        return set()

    local_seasons: dict[int, set[int]] = {}
    for show_id, season_number in Item.objects.filter(
        parent_id__in=show_ids,
        media_type=MediaType.SEASON.value,
    ).values_list("parent_id", "season_number"):
        if season_number is not None:
            local_seasons.setdefault(show_id, set()).add(season_number)

    today_date = timezone.localdate()
    aired_items = Q(release_date__isnull=True) | Q(release_date__lte=today_date)
    episode_totals = dict(
        Item.objects.filter(
            aired_items,
            media_type=MediaType.EPISODE.value,
            parent__parent_id__in=show_ids,
        )
        .values("parent__parent_id")
        .annotate(total=Count("pk"))
        .values_list("parent__parent_id", "total")
    )
    aired_tracking = Q(item__release_date__isnull=True) | Q(
        item__release_date__lte=today_date
    )
    watched_totals = dict(
        Episode.objects.filter(
            aired_tracking,
            user=user,
            status=Status.COMPLETED.value,
            item__parent__parent_id__in=show_ids,
        )
        .values("item__parent__parent_id")
        .annotate(total=Count("pk"))
        .values_list("item__parent__parent_id", "total")
    )

    today = today_date.isoformat()
    complete = set()
    for show in shows:
        has_unfetched_aired_season = any(
            raw_season.get("season_number")
            and raw_season.get("season_number") not in local_seasons.get(show.pk, set())
            and raw_season.get("air_date")
            and raw_season["air_date"] <= today
            for raw_season in (show.raw or {}).get("seasons", [])
        )
        episode_total = episode_totals.get(show.pk, 0)
        if (
            episode_total > 0
            and watched_totals.get(show.pk, 0) >= episode_total
            and not has_unfetched_aired_season
        ):
            complete.add(show.pk)
    return complete


def _discontinued_cards(user) -> list[dict]:
    """Hidden shows that are not fully watched."""
    from tracking.models import ShowFeedPreference

    preferences = list(
        ShowFeedPreference.objects.filter(
            user=user,
            up_next_hidden_at__isnull=False,
            show__media_type=MediaType.TV.value,
        )
        .select_related("show")
        .order_by("-up_next_hidden_at", "show__title")
    )
    fully_watched = _fully_watched_show_ids(user, [pref.show for pref in preferences])
    return [
        {**_card(pref.show), "item_id": pref.show_id}
        for pref in preferences
        if pref.show_id not in fully_watched
    ]


@login_required
def discontinued(request):
    return render(
        request,
        "core/discontinued.html",
        {"cards": _discontinued_cards(request.user)},
    )


@login_required
@require_http_methods(["POST"])
def restore_discontinued(request, item_id: int):
    """Allow a discontinued show back into Up Next without touching history."""
    from tracking import services

    show = get_object_or_404(Item, pk=item_id, media_type=MediaType.TV.value)
    services.set_show_visibility(user=request.user, show=show, up_next=False)

    if not request.headers.get("HX-Request"):
        return redirect("core:discontinued")
    return render(
        request,
        "core/partials/_discontinued_content.html",
        {"cards": _discontinued_cards(request.user)},
    )


@login_required
def favorites(request):
    media_type = request.GET.get("type", "all")
    qs = Favorite.objects.filter(user=request.user).select_related(
        "item", "item__parent", "item__parent__parent"
    )
    if media_type != "all":
        # "Shows" covers episodes too, otherwise a starred episode is
        # unreachable from every filter tab.
        types = (
            [MediaType.TV.value, MediaType.SEASON.value, MediaType.EPISODE.value]
            if media_type == MediaType.TV.value
            else [media_type]
        )
        qs = qs.filter(item__media_type__in=types)

    cards = []
    for fav in qs:
        item = fav.item
        subtitle = ""
        if item.media_type == MediaType.EPISODE.value:
            subtitle = f"S{item.season_number:02d}E{item.episode_number:02d}"
        cards.append(_card(item, subtitle))

    return render(
        request,
        "core/favorites.html",
        {
            "cards": cards,
            "media_type": media_type,
            "filters": FILTERS,
        },
    )


@login_required
@require_http_methods(["POST"])
def toggle_favorite(request, item_id: int):
    """Star/unstar one item. Returns the re-rendered star button."""
    item = get_object_or_404(Item, pk=item_id)
    deleted, _ = Favorite.objects.filter(user=request.user, item=item).delete()
    if not deleted:
        Favorite.objects.create(user=request.user, item=item)
    if not request.headers.get("HX-Request"):
        return HttpResponse(status=204)
    return render(
        request,
        "core/partials/_favorite_button.html",
        {
            "item": item,
            "is_favorite": not deleted,
        },
    )
