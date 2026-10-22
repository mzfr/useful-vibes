"""
Releases — cached, per-item "what's already out that you haven't watched".

We don't poll. We populate the cache lazily when the user visits /releases.
Stale entries (> 24h) get refreshed inline against TMDB; fresh entries come
from the cache row's payload_json.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import Item


class ReleaseCheck(models.Model):
    """One cache row per (user, item-they-track). payload_json holds the result."""

    TTL = timedelta(hours=24)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="release_checks"
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="release_checks")
    last_checked_at = models.DateTimeField(auto_now=True)
    payload_json = models.JSONField(default=dict, blank=True)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "item"], name="releasecheck_user_item_unique"),
        ]
        indexes = [models.Index(fields=["user", "last_checked_at"])]

    def __str__(self) -> str:
        return f"ReleaseCheck<{self.item_id}> @ {self.last_checked_at:%Y-%m-%d %H:%M}"

    def is_fresh(self) -> bool:
        return timezone.now() - self.last_checked_at < self.TTL
