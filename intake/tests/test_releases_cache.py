"""Releases: just the cache freshness logic (the heavy lifting is provider-bound)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.enums import MediaType
from core.models import Item
from releases.models import ReleaseCheck

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def show(user):
    return Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="42", title="X"
    )


def test_fresh_within_ttl(user, show):
    rc = ReleaseCheck.objects.create(user=user, item=show)
    assert rc.is_fresh()


def test_stale_after_ttl(user, show):
    rc = ReleaseCheck.objects.create(user=user, item=show)
    # auto_now would clobber an instance assignment on save(), so bypass it
    # by writing through queryset.update().
    ReleaseCheck.objects.filter(pk=rc.pk).update(
        last_checked_at=timezone.now() - timedelta(hours=25)
    )
    rc.refresh_from_db()
    assert not rc.is_fresh()


def test_unique_per_user_item(user, show):
    ReleaseCheck.objects.create(user=user, item=show)
    from django.db import IntegrityError
    with pytest.raises(IntegrityError):
        ReleaseCheck.objects.create(user=user, item=show)
