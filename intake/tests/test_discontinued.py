from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import CompletionEvent, Episode, ShowFeedPreference
from tracking.services import set_show_visibility
from tracking.up_next import shows_for_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="discontinued")


def _show_with_progress(user, media_id="880001"):
    show = Item.objects.create(
        source="tmdb",
        media_type=MediaType.TV.value,
        media_id=media_id,
        title="Abandoned Show",
        metadata_complete=True,
    )
    season = Item.objects.create(
        source="tmdb",
        media_type=MediaType.SEASON.value,
        media_id=media_id,
        season_number=1,
        parent=show,
        title="Season 1",
    )
    watched = Item.objects.create(
        source="tmdb",
        media_type=MediaType.EPISODE.value,
        media_id=media_id,
        season_number=1,
        episode_number=1,
        parent=season,
        title="Watched Pilot",
        release_date=date.today() - timedelta(days=2),
    )
    next_episode = Item.objects.create(
        source="tmdb",
        media_type=MediaType.EPISODE.value,
        media_id=media_id,
        season_number=1,
        episode_number=2,
        parent=season,
        title="Unwatched Follow-up",
        release_date=date.today() - timedelta(days=1),
    )
    Episode.objects.create(
        user=user,
        item=watched,
        status=Status.COMPLETED.value,
        progressed_at=timezone.now(),
    )
    return show, watched, next_episode


def test_discontinuing_an_episode_hides_its_whole_show_and_keeps_history(client, user):
    show, watched, next_episode = _show_with_progress(user)
    history_count = CompletionEvent.objects.filter(user=user, item=watched).count()
    client.force_login(user)

    response = client.post(
        reverse("tracking:up_next_discontinue", args=[next_episode.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert ShowFeedPreference.objects.filter(
        user=user, show=show, up_next_hidden_at__isnull=False
    ).exists()
    assert shows_for_user(user) == []
    tracked = Episode.objects.get(user=user, item=watched)
    assert tracked.status == Status.COMPLETED.value
    assert CompletionEvent.objects.filter(user=user, item=watched).count() == history_count

    detail = client.get(reverse("tracking:details", args=[show.media_id]))
    assert "Watched Pilot" in detail.content.decode()


def test_discontinued_page_lists_show_and_restore_returns_it_to_up_next(client, user):
    show, _watched, _next_episode = _show_with_progress(user)
    set_show_visibility(user=user, show=show, up_next=True, releases=True)

    completed_show, _first, last = _show_with_progress(user, media_id="880002")
    completed_show.title = "Completed Show"
    completed_show.save()
    Episode.objects.create(
        user=user,
        item=last,
        status=Status.COMPLETED.value,
        progressed_at=timezone.now(),
    )
    set_show_visibility(user=user, show=completed_show, up_next=True)

    never_started = Item.objects.create(
        source="tmdb",
        media_type=MediaType.TV.value,
        media_id="880003",
        title="Never Started Show",
        metadata_complete=True,
    )
    set_show_visibility(user=user, show=never_started, up_next=True)
    client.force_login(user)

    page = client.get(reverse("core:discontinued"))
    body = page.content.decode()
    assert page.status_code == 200
    assert "Abandoned Show" in body
    assert "Never Started Show" in body
    assert "Completed Show" not in body
    assert "Move Abandoned Show back to Up Next" in body

    response = client.post(
        reverse("core:restore_discontinued", args=[show.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    pref = ShowFeedPreference.objects.get(user=user, show=show)
    assert pref.up_next_hidden_at is None
    assert pref.releases_hidden_at is not None
    assert [row.show_item for row in shows_for_user(user)] == [show]
    assert "Abandoned Show" not in response.content.decode()


def test_show_detail_discontinued_switch_changes_up_next_visibility(client, user):
    show, _watched, _next_episode = _show_with_progress(user)
    url = reverse("tracking:show_visibility", args=[show.media_id])
    client.force_login(user)

    discontinued = client.post(url, {"field": "up_next", "on": "0"})
    assert discontinued.status_code == 200
    assert ShowFeedPreference.objects.filter(
        user=user, show=show, up_next_hidden_at__isnull=False
    ).exists()
    assert 'aria-checked="true"' in discontinued.content.decode()

    restored = client.post(url, {"field": "up_next", "on": "1"})
    assert restored.status_code == 200
    assert not ShowFeedPreference.objects.filter(user=user, show=show).exists()
    assert 'aria-checked="false"' in restored.content.decode()
