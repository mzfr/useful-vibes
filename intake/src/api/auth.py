"""
Ninja auth — accepts either:
  - `Authorization: Bearer <token>` from a minted APIToken (checked first), or
  - a session cookie (same-origin browser use).

Ordering matters at the NinjaAPI mount: django_auth raises 403 on CSRF-less
POSTs, so TokenAuth must run first (it returns None without a bearer header,
falling through cleanly).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from ninja.security import HttpBearer

from .models import APIToken

# Only persist last_used_at if it's this stale — avoids a DB write per request.
_TOUCH_INTERVAL = timedelta(minutes=5)


class TokenAuth(HttpBearer):
    def authenticate(self, request, token):
        record = APIToken.lookup(token)
        if record is None:
            return None
        if not record.user.is_active:
            return None
        now = timezone.now()
        if record.last_used_at is None or now - record.last_used_at > _TOUCH_INTERVAL:
            record.last_used_at = now
            record.save(update_fields=["last_used_at"])
        request.user = record.user
        return record.user
