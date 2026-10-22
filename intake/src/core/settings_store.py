from __future__ import annotations

from django.conf import settings
from django.db import OperationalError, ProgrammingError


def tmdb_api_key() -> str:
    """Return the encrypted DB value, falling back to the environment."""
    from .models import AppSettings

    try:
        stored = AppSettings.objects.filter(singleton=True).first()
    except (OperationalError, ProgrammingError, RuntimeError):
        stored = None
    if stored:
        value = stored.get_tmdb_api_key()
        if value:
            return value
    return settings.TMDB_API_KEY or ""


def history_public_url() -> str:
    """Return the configured history-share origin, falling back to the environment."""
    from .models import AppSettings

    try:
        stored = AppSettings.objects.filter(singleton=True).first()
    except (OperationalError, ProgrammingError, RuntimeError):
        stored = None
    if stored and stored.history_public_url:
        return stored.history_public_url.rstrip("/")
    return (settings.INTAKE_HISTORY_PUBLIC_URL or "").rstrip("/")


def history_sync_interval() -> tuple[int, str, int]:
    """Return the history sync value, unit, and equivalent seconds."""
    from .models import AppSettings

    try:
        stored = AppSettings.objects.filter(singleton=True).first()
    except (OperationalError, ProgrammingError, RuntimeError):
        stored = None
    value = max(1, stored.history_sync_interval_value) if stored else 12
    unit = stored.history_sync_interval_unit if stored else "hours"
    if unit not in {"minutes", "hours"}:
        unit = "hours"
    seconds = value * (60 if unit == "minutes" else 3600)
    return value, unit, seconds


DEFAULT_SOURCE_ADDON_URL = "https://torrentio.strem.fun"


def source_addon_url() -> str:
    """Stremio addon base URL for stream lookup, defaulting to Torrentio."""
    from .models import AppSettings

    try:
        stored = AppSettings.objects.filter(singleton=True).first()
    except (OperationalError, ProgrammingError, RuntimeError):
        stored = None
    if stored and stored.source_addon_url:
        return stored.source_addon_url
    return DEFAULT_SOURCE_ADDON_URL


DEFAULT_QBITTORRENT_HOST = "127.0.0.1"
DEFAULT_QBITTORRENT_PORT = 8080
DEFAULT_QBITTORRENT_USERNAME = "admin"


def qbittorrent_config() -> dict[str, str | int | None]:
    """qBittorrent Web API connection details from the DB, with defaults.

    Returns {"host", "port", "username", "password"} — password decrypts to ""
    when unset. Missing host/port fall back to the classic defaults so the
    Sources panel can be left half-configured without breaking lookups.
    """
    from .models import AppSettings

    try:
        stored = AppSettings.objects.filter(singleton=True).first()
    except (OperationalError, ProgrammingError, RuntimeError):
        stored = None
    host = (stored.qbittorrent_host if stored else "") or DEFAULT_QBITTORRENT_HOST
    port = (
        stored.qbittorrent_port if stored and stored.qbittorrent_port else DEFAULT_QBITTORRENT_PORT
    )
    username = (stored.qbittorrent_username if stored else "") or DEFAULT_QBITTORRENT_USERNAME
    password = stored.get_secret("qbittorrent_password") if stored else ""
    return {"host": host, "port": port, "username": username, "password": password}
