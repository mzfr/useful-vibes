"""
Personal access tokens.

One token at a time per user — generating a new one revokes the old.
Stored as a SHA-256 digest; the raw token is only shown once at mint time.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_raw_token() -> str:
    return "intake_" + secrets.token_urlsafe(24)


class APIToken(models.Model):
    TTL_PRESETS = (
        ("1d", "1 day"),
        ("1w", "1 week"),
        ("30d", "30 days"),
        ("1y", "1 year"),
        ("never", "Never"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_token"
    )
    digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    label = models.CharField(max_length=120, blank=True, default="")

    def __str__(self) -> str:
        return f"APIToken<{self.user_id}> ({self.label or 'unlabelled'})"

    @classmethod
    def expiry_for_ttl(cls, ttl: str):
        now = timezone.now()
        return {
            "1d": now + timedelta(days=1),
            "1w": now + timedelta(weeks=1),
            "30d": now + timedelta(days=30),
            "1y": now + timedelta(days=365),
            "never": None,
        }.get(ttl, now + timedelta(days=30))

    @classmethod
    def mint(cls, user, *, ttl: str = "30d", label: str = "") -> tuple[APIToken, str]:
        """Generate (or replace) the user's token. Returns (model, raw_token)."""
        raw = _make_raw_token()
        cls.objects.filter(user=user).delete()
        token = cls.objects.create(
            user=user,
            digest=_digest(raw),
            expires_at=cls.expiry_for_ttl(ttl),
            label=label,
        )
        return token, raw

    @classmethod
    def lookup(cls, raw: str):
        token = cls.objects.filter(digest=_digest(raw)).select_related("user").first()
        if token is None:
            return None
        if token.expires_at is not None and token.expires_at < timezone.now():
            return None
        return token


