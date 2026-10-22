"""
Server-rendered HTMX views for search, details, and tracking.

These are the primary UX surface — the REST API in Phase 7 is parallel, not
the source of truth for the templates.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from core.enums import MediaType, Status
from core.models import Item
from providers.base import NotSupported, ProviderError

from . import services
from .forms import TrackForm
from .models import MODEL_FOR_MEDIA_TYPE


@login_required
def clean_details(request, media_id: int, movie: bool = False):
    """Resolve a clean TMDB ID route, preferring TV for namespace collisions."""
    media_types = [MediaType.MOVIE.value] if movie else [MediaType.TV.value, MediaType.MOVIE.value]
    item = None
    for media_type in media_types:
        item = Item.objects.filter(
            source="tmdb",
            media_type=media_type,
            media_id=str(media_id),
            season_number__isnull=True,
            episode_number__isnull=True,
        ).first()
        if item is not None:
            return details(request, "tmdb", media_type, str(media_id))
    raise Http404("No local TMDB movie or show has this ID")


@login_required
def legacy_details(request, source: str, media_type: str, media_id: str):
    """Redirect provider-qualified URLs to their clean detail pages."""
    if source == "openlibrary" and media_type == MediaType.BOOK.value:
        item = get_object_or_404(
            Item,
            source=source,
            media_type=media_type,
            media_id=media_id,
        )
        return HttpResponsePermanentRedirect(item.get_absolute_url())
    if source == "tmdb":
        try:
            item, _metadata = services.resolve_root_item(source, media_type, media_id)
        except (NotSupported, ProviderError) as exc:
            return render(request, "tracking/details_error.html", {"error": str(exc)}, status=502)
        return HttpResponsePermanentRedirect(item.get_absolute_url())
    return details(request, source, media_type, media_id)


@login_required
def legacy_episode(request, source: str, media_id: str, season_number: int, episode_number: int):
    episode = get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.EPISODE.value,
        media_id=str(media_id),
        season_number=season_number,
        episode_number=episode_number,
    )
    return HttpResponsePermanentRedirect(episode.get_absolute_url())


@login_required
def legacy_season(request, source: str, media_id: str, season_number: int):
    show = get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.TV.value,
        media_id=str(media_id),
        season_number__isnull=True,
        episode_number__isnull=True,
    )
    return HttpResponsePermanentRedirect(show.get_absolute_url())


@login_required
def details(request, source: str, media_type: str, media_id: str):
    """Details page. Reads local catalog first; only hits TMDB if missing.

    TV shows and movies use dedicated detail pages. Books retain the generic
    tracking form until their own detail design is introduced.
    """
    try:
        item, metadata = services.resolve_root_item(source, media_type, media_id)
    except (NotSupported, ProviderError) as exc:
        return render(request, "tracking/details_error.html", {"error": str(exc)}, status=502)

    if media_type == MediaType.TV.value:
        from .show_detail import build_show_context

        ctx = build_show_context(request.user, item)
        raw = item.raw or {}
        ctx["show_network"] = ", ".join(
            n.get("name", "") for n in raw.get("networks", []) if n.get("name")
        )
        ctx["show_status"] = raw.get("status", "")
        return render(request, "tracking/show_detail.html", ctx)

    if media_type == MediaType.MOVIE.value:
        from .movie_detail import build_movie_context

        return render(
            request,
            "tracking/movie_detail.html",
            build_movie_context(request.user, item),
        )

    model = MODEL_FOR_MEDIA_TYPE[media_type]
    tracking = model.objects.filter(user=request.user, item=item).first()

    # Build a Metadata-shaped object for the template when serving from cache.
    if metadata is None:
        from providers.base import Metadata

        metadata = Metadata(
            source=item.source,
            media_type=item.media_type,
            media_id=item.media_id,
            title=item.title,
            image=item.image,
            synopsis=item.synopsis,
            genres=[],
            release_date=item.release_date,
            runtime=item.runtime,
            max_progress=item.max_progress,
            external_ids={},
            raw=item.raw or {},
        )

    return render(
        request,
        "tracking/details.html",
        {
            "metadata": metadata,
            "item": item,
            "tracking": tracking,
        },
    )


@login_required
@require_http_methods(["POST"])
def track(request, source: str, media_type: str, media_id: str):
    """Create or update a tracking row.

    Triggered by the track-modal form. Returns the updated tracking partial
    when called via HTMX, otherwise redirects back to the details page.
    """
    form = TrackForm(request.POST)
    if not form.is_valid():
        return HttpResponse("invalid form", status=400)

    if media_type not in MODEL_FOR_MEDIA_TYPE:
        return HttpResponse(f"unknown media type {media_type!r}", status=400)

    try:
        item, _ = services.resolve_root_item(source, media_type, media_id)
    except (NotSupported, ProviderError) as exc:
        return HttpResponse(f"provider error: {exc}", status=502)

    services.update_tracking(
        user=request.user,
        item=item,
        score=form.cleaned_data.get("score"),
        progress=form.cleaned_data.get("progress"),
        status=form.cleaned_data.get("status") or None,
        start_date=form.cleaned_data.get("start_date"),
        end_date=form.cleaned_data.get("end_date"),
    )

    if request.headers.get("HX-Request"):
        # Re-render the track-status partial inline.
        model = MODEL_FOR_MEDIA_TYPE[media_type]
        tracking = model.objects.filter(user=request.user, item=item).first()
        return render(
            request,
            "tracking/_track_status.html",
            {"item": item, "tracking": tracking},
        )

    return redirect(item.get_absolute_url())


@login_required
@require_http_methods(["POST"])
def show_visibility(request, source: str, media_id: str):
    """Flip one visibility switch. Re-renders just the toggle row in the
    header banner (`#visibility-toggles`).

    POST `field` is "up_next" or "releases"; `on` is the switch's state
    before this click ("1"/"0"). The Up Next switch represents the positive
    "Discontinued" state, while Releases represents visible/enabled.
    """
    show = get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.TV.value,
        media_id=media_id,
        season_number__isnull=True,
        episode_number__isnull=True,
    )
    field = request.POST.get("field", "")
    was_on = request.POST.get("on") == "1"
    if field not in ("up_next", "releases"):
        return HttpResponse(f"unknown visibility field {field!r}", status=400)

    hidden = not was_on if field == "up_next" else was_on
    services.set_show_visibility(user=request.user, show=show, **{field: hidden})

    from .show_detail import build_show_context

    ctx = build_show_context(request.user, show)
    return render(request, "tracking/partials/_visibility_toggles.html", ctx)


@login_required
@require_http_methods(["POST"])
def mark_show_next_watched(request, source: str, media_id: str):
    """Mark the show's next-up episode watched from the show header button."""
    from .show_detail import build_show_context, mark_next_episode_watched

    show = get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.TV.value,
        media_id=media_id,
        season_number__isnull=True,
        episode_number__isnull=True,
    )
    mark_next_episode_watched(request.user, show)
    ctx = build_show_context(request.user, show)
    return render(request, "tracking/partials/_show_body.html", ctx)


@login_required
@require_http_methods(["POST"])
def toggle_watchlist(request, source: str, media_id: str):
    """Toggle "on watchlist" — the TV tracking row's status, planning <-> untracked.

    There's no separate watchlist model: Status.PLANNING already means "want
    to watch, haven't started" for every media type in this app, so the
    Watchlist button just writes/clears that status.
    """
    show = get_object_or_404(
        Item,
        source=source,
        media_type=MediaType.TV.value,
        media_id=media_id,
        season_number__isnull=True,
        episode_number__isnull=True,
    )
    _flip_planning(request.user, show)

    from .show_detail import build_show_context

    ctx = build_show_context(request.user, show)
    return render(request, "tracking/partials/_show_body.html", ctx)


@login_required
@require_http_methods(["POST"])
def remove_from_watchlist(request, item_id: int):
    """Explicitly remove an item from the watchlist.

    The watchlist is made of PLANNING rows, so removal deletes that row.
    Non-planning rows are already absent and remain untouched to preserve
    progress and watch history.
    """
    item = get_object_or_404(Item, pk=item_id)
    if item.media_type not in MODEL_FOR_MEDIA_TYPE:
        return HttpResponse(f"cannot watchlist {item.media_type!r}", status=400)

    model = MODEL_FOR_MEDIA_TYPE[item.media_type]
    row = model.objects.filter(user=request.user, item=item).first()
    if row is not None and row.status == Status.PLANNING.value:
        row.delete()
    # Non-planning rows are not on the watchlist to begin with — leave them
    # (and their watch history) alone rather than demoting them.
    if not request.headers.get("HX-Request"):
        return HttpResponse(status=204)
    return render(
        request,
        "tracking/partials/_watchlist_button.html",
        {
            "item": item,
            "on_watchlist": False,
        },
    )


def _flip_planning(user, item: Item) -> bool:
    """Toggle Status.PLANNING on `item`'s tracking row. Returns the new state.

    PLANNING *is* the watchlist (there is no separate flag), so a row that is
    completed / in-progress / paused / dropped can't join it — demoting would
    erase real watch history. Those rows are left untouched instead.
    """
    model = MODEL_FOR_MEDIA_TYPE[item.media_type]
    row = model.objects.filter(user=user, item=item).first()
    if row is None:
        services.update_tracking(user=user, item=item, status="planning")
        return True
    if row.status == "planning":
        row.delete()
        return False
    return False


@login_required
@require_http_methods(["POST"])
def watchlist_toggle(request, item_id: int):
    """Watchlist toggle for any media type, used by the search results grid."""
    item = get_object_or_404(Item, pk=item_id)
    if item.media_type not in MODEL_FOR_MEDIA_TYPE:
        return HttpResponse(f"cannot watchlist {item.media_type!r}", status=400)
    on = _flip_planning(request.user, item)
    return render(
        request,
        "tracking/partials/_watchlist_button.html",
        {
            "item": item,
            "on_watchlist": on,
        },
    )


@login_required
@require_http_methods(["POST"])
def add_remote(request, source: str, media_type: str, media_id: str):
    """Import a provider search hit into the local catalog, then watchlist it.

    This is the "I watched a trailer for something unreleased" path: the item
    has no local row yet, so it has to be resolved from TMDB first.
    """
    if media_type not in MODEL_FOR_MEDIA_TYPE:
        return HttpResponse(f"unknown media type {media_type!r}", status=400)
    try:
        item, _ = services.resolve_root_item(source, media_type, media_id)
    except (NotSupported, ProviderError) as exc:
        return HttpResponse(f"provider error: {exc}", status=502)

    on = _flip_planning(request.user, item)
    return render(
        request,
        "tracking/partials/_watchlist_button.html",
        {
            "item": item,
            "on_watchlist": on,
        },
    )


@login_required
@require_http_methods(["POST"])
def watched_again(request, source: str, media_type: str, media_id: str):
    """Bump the repeats counter for a completed item."""
    if media_type not in MODEL_FOR_MEDIA_TYPE:
        return HttpResponse(f"unknown media type {media_type!r}", status=400)

    try:
        item, _ = services.resolve_root_item(source, media_type, media_id)
    except (NotSupported, ProviderError) as exc:
        return HttpResponse(f"provider error: {exc}", status=502)

    model = MODEL_FOR_MEDIA_TYPE[media_type]
    row = model.objects.filter(user=request.user, item=item, status="completed").first()
    if row is None:
        return HttpResponse("complete the item before marking it watched again", status=400)
    row.mark_watched_again()

    if request.headers.get("HX-Request"):
        return render(
            request,
            "tracking/_track_status.html",
            {"item": item, "tracking": row},
        )
    return redirect(item.get_absolute_url())
