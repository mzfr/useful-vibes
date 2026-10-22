"""Versioned, lossless JSON export helpers."""
from __future__ import annotations

from tracking.models import TV, Book, Episode, Movie, Season, ShowFeedPreference

from .models import TraktLegacy

MODELS = {
    "movies": Movie,
    "shows": TV,
    "seasons": Season,
    "episodes": Episode,
    "books": Book,
}


def _date(value):
    return value.isoformat() if value else None


def export_payload(user) -> dict:
    payload = {"schema_version": 3, "tracking": {}}
    for name, model in MODELS.items():
        rows = []
        for row in model.objects.filter(user=user).select_related("item"):
            current = {
                "source": row.item.source,
                "media_type": row.item.media_type,
                "media_id": row.item.media_id,
                "season_number": row.item.season_number,
                "episode_number": row.item.episode_number,
                "title": row.item.title,
                "status": row.status,
                "score": row.score,
                "progress": row.progress,
                "progressed_at": _date(row.progressed_at),
                "repeats": row.repeats,
                "start_date": _date(row.start_date),
                "end_date": _date(row.end_date),
            }
            current["history"] = [
                {
                    "history_date": _date(snapshot.history_date),
                    "history_type": snapshot.history_type,
                    "status": snapshot.status,
                    "score": snapshot.score,
                    "progress": snapshot.progress,
                    "progressed_at": _date(snapshot.progressed_at),
                    "repeats": snapshot.repeats,
                    "start_date": _date(snapshot.start_date),
                    "end_date": _date(snapshot.end_date),
                }
                for snapshot in row.history.order_by("history_date", "history_id")
            ]
            rows.append(current)
        payload["tracking"][name] = rows
        # Keep v1 top-level keys for existing API consumers while the versioned
        # `tracking` envelope becomes the canonical archive shape.
        payload[name] = rows
    payload["show_feed_preferences"] = [
        {
            "source": pref.show.source,
            "media_id": pref.show.media_id,
            "up_next_hidden_at": _date(pref.up_next_hidden_at),
            "releases_hidden_at": _date(pref.releases_hidden_at),
        }
        for pref in ShowFeedPreference.objects.filter(user=user).select_related("show")
    ]
    legacy = TraktLegacy.objects.filter(user=user).first()
    payload["trakt_legacy"] = legacy.metadata if legacy else None
    return payload
