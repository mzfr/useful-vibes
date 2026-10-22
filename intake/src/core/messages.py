"""
Thin helpers for the persistent toast queue (`UserMessage`).

We deliberately don't use Django's `django.contrib.messages` because that uses
session storage — toasts disappear if the user opens a new tab. `UserMessage`
rows survive across reloads and HTMX swaps; the toast partial drains them on
render and marks them seen so they show exactly once.
"""
from __future__ import annotations

from django.utils import timezone

from .enums import MessageLevel
from .models import UserMessage


def push(user, text: str, level: str = MessageLevel.INFO.value) -> UserMessage:
    """Queue a toast for the user. Always returns the row so callers can chain."""
    return UserMessage.objects.create(user=user, level=level, text=text)


def info(user, text: str) -> UserMessage:
    return push(user, text, MessageLevel.INFO.value)


def success(user, text: str) -> UserMessage:
    return push(user, text, MessageLevel.SUCCESS.value)


def warning(user, text: str) -> UserMessage:
    return push(user, text, MessageLevel.WARNING.value)


def error(user, text: str) -> UserMessage:
    return push(user, text, MessageLevel.ERROR.value)


def drain(user) -> list[UserMessage]:
    """Return all unseen messages and mark them seen in one trip.

    Idempotent: drains an empty list cleanly if nothing's queued.
    """
    rows = list(UserMessage.objects.filter(user=user, seen_at__isnull=True))
    if rows:
        ids = [r.pk for r in rows]
        UserMessage.objects.filter(pk__in=ids).update(seen_at=timezone.now())
    return rows
