"""Private management UI for static public-history links.

There is deliberately no public Django endpoint. Active links are rendered to
plain HTML by ``render_history_site`` and uploaded to Cloudflare Pages.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core import messages as user_messages
from core.models import AppSettings
from core.settings_store import history_public_url, history_sync_interval

from .models import AccessLink


@login_required
def manage(request):
    origin = history_public_url()
    sync_value, sync_unit, _sync_seconds = history_sync_interval()
    links = [
        {
            "link": link,
            "url": f"{origin}/s/{link.token}/",
            "active": link.is_active(),
        }
        for link in AccessLink.objects.filter(owner=request.user, scope="history")
    ]
    return render(
        request,
        "sharing/manage.html",
        {
            "links": links,
            "public_origin": origin,
            "sync_value": sync_value,
            "sync_unit": sync_unit,
            "sync_interval_label": (
                f"{sync_value} {sync_unit[:-1] if sync_value == 1 else sync_unit}"
            ),
        },
    )


def _normalize_public_origin(value: str) -> str:
    candidate = value.strip().rstrip("/")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        URLValidator(schemes=["https"])(candidate)
        parsed = urlsplit(candidate)
        port = parsed.port
    except (ValidationError, ValueError) as exc:
        raise ValidationError("Enter a valid HTTPS domain.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("Enter an HTTPS origin without a path, query, or fragment.")
    return f"https://{parsed.hostname.lower()}"


@login_required
@require_http_methods(["POST"])
def save_origin(request):
    try:
        origin = _normalize_public_origin(request.POST.get("history_public_url", ""))
    except ValidationError as exc:
        user_messages.error(request.user, exc.messages[0])
        return redirect(reverse("sharing:manage"))

    app_settings = AppSettings.load()
    app_settings.history_public_url = origin
    app_settings.save(update_fields=["history_public_url", "updated_at"])
    user_messages.success(request.user, "Public history domain updated.")
    return redirect(reverse("sharing:manage"))


@login_required
@require_http_methods(["POST"])
def save_sync_interval(request):
    value_raw = request.POST.get("sync_value", "").strip()
    unit = request.POST.get("sync_unit", "")
    try:
        value = int(value_raw)
    except ValueError:
        value = 0
    if value < 1 or unit not in {"minutes", "hours"}:
        user_messages.error(request.user, "Choose a positive interval in minutes or hours.")
        return redirect(reverse("sharing:manage"))

    app_settings = AppSettings.load()
    app_settings.history_sync_interval_value = value
    app_settings.history_sync_interval_unit = unit
    app_settings.save(
        update_fields=[
            "history_sync_interval_value",
            "history_sync_interval_unit",
            "updated_at",
        ]
    )
    label = f"{value} {unit[:-1] if value == 1 else unit}"
    user_messages.success(request.user, f"History sync interval updated to {label}.")
    return redirect(reverse("sharing:manage"))


@login_required
@require_http_methods(["POST"])
def create(request):
    ttl = request.POST.get("ttl", "never")
    if ttl not in {value for value, _label in AccessLink.TTL_PRESETS}:
        return HttpResponseBadRequest("invalid expiry")
    label = (request.POST.get("label") or "").strip()
    AccessLink.objects.create(
        owner=request.user,
        scope="history",
        label=label,
        expires_at=AccessLink.expiry_for_ttl(ttl),
    )
    user_messages.success(
        request.user,
        "History share created. It will appear after the next scheduled sync.",
    )
    return redirect(reverse("sharing:manage"))


@login_required
@require_http_methods(["POST"])
def revoke(request, link_id: int):
    link = get_object_or_404(
        AccessLink,
        pk=link_id,
        owner=request.user,
        scope="history",
    )
    link.revoked_at = timezone.now()
    link.save(update_fields=["revoked_at"])
    user_messages.success(
        request.user,
        "History share revoked. It will disappear after the next 12-hour sync.",
    )
    return redirect(reverse("sharing:manage"))
