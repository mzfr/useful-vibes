"""Opaque access links for static public-history exports.

Only the ``history`` scope is created by the current UI. Older library-share
rows can remain in the database but are ignored by the static renderer.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def _make_token() -> str:
    return secrets.token_urlsafe(24)


class AccessLink(models.Model):
    TTL_PRESETS = (
        ("1d", "1 day"),
        ("1w", "1 week"),
        ("1m", "1 month"),
        ("never", "Never"),
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="access_links"
    )
    token = models.CharField(max_length=64, unique=True, default=_make_token)
    label = models.CharField(max_length=120, blank=True, default="")
    scope = models.CharField(max_length=64, default="history")
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_visited_at = models.DateTimeField(null=True, blank=True)
    visit_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.label or self.scope} ({self.token[:6]}…)"

    @classmethod
    def expiry_for_ttl(cls, ttl: str):
        now = timezone.now()
        return {
            "1d": now + timedelta(days=1),
            "1w": now + timedelta(weeks=1),
            "1m": now + timedelta(days=30),
            "never": None,
        }.get(ttl, now + timedelta(days=1))

    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at < timezone.now():
            return False
        return True
