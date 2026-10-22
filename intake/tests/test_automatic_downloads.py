"""Automatic episode selection, delivery, and Up Next toggle."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from sources.automatic import ProcessResult, choose_stream, process_subscription
from sources.base import Stream
from tracking.models import AutomaticEpisodeDownload, Episode, ShowAutomaticDownload

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="auto", password="p")


@pytest.fixture
def show_episodes(user):
    show = Item.objects.create(
        source="tmdb",
        media_type=MediaType.TV.value,
        media_id="700",
        title="Automatic Show",
        metadata_complete=True,
        raw={"external_ids": {"imdb_id": "tt0000700"}},
    )
    season = Item.objects.create(
        source="tmdb",
        media_type=MediaType.SEASON.value,
        media_id="700",
        season_number=1,
        parent=show,
        title="Season 1",
    )
    today = timezone.localdate()
    episodes = []
    for number in range(1, 11):
        episode = Item.objects.create(
            source="tmdb",
            media_type=MediaType.EPISODE.value,
            media_id="700",
            season_number=1,
            episode_number=number,
            parent=season,
            title=f"Episode {number}",
            release_date=today if number <= 9 else today + timedelta(days=1),
        )
        episodes.append(episode)
    for episode in episodes[:4]:
        Episode.objects.create(
            user=user,
            item=episode,
            status=Status.COMPLETED.value,
            progressed_at=timezone.now(),
        )
    return show, episodes


def test_choose_stream_uses_seeders_after_size_and_quality_rules():
    streams = [
        Stream("too large", "a" * 40, seeders=500, size_mb=10241, quality="1080p"),
        Stream("too low", "b" * 40, seeders=400, size_mb=900, quality="480p"),
        Stream("usable", "c" * 40, seeders=30, size_mb=2000, quality="1080p"),
        Stream("less seeded", "d" * 40, seeders=20, size_mb=1000, quality="720p"),
    ]

    assert choose_stream(streams).title == "usable"


@pytest.mark.parametrize("size", [None, 1023, 10241])
def test_rejects_unknown_and_out_of_range_sizes(size):
    assert choose_stream([Stream("release", "a" * 40, size_mb=size)]) is None


@pytest.mark.parametrize("size", [1024, 10240])
def test_accepts_size_boundaries(size):
    stream = Stream("release", "a" * 40, size_mb=size)
    assert choose_stream([stream]) is stream


def test_automatic_save_path_authentication():
    from urllib.parse import parse_qs

    import respx
    from httpx import Response

    from sources.download_client import DownloadClient

    client = DownloadClient()
    with respx.mock:
        preferences = respx.get(client._base + "/api/v2/app/preferences").mock(
            side_effect=[Response(403), Response(200, json={"save_path": "/mnt/hdd/torrents"})]
        )
        respx.post(client._base + "/api/v2/auth/login").mock(
            return_value=Response(200, text="Ok.")
        )
        add = respx.post(client._base + "/api/v2/torrents/add").mock(
            return_value=Response(200, text="Ok.")
        )
        client.add_magnet("magnet:?xt=urn:btih:" + "a" * 40,
                          show_title="School Spirits", season_number=2)
        payload = parse_qs(add.calls[0].request.content.decode())
        assert payload["savepath"] == ["/mnt/hdd/torrents/School Spirits/Season 02"]
        assert payload["autoTMM"] == ["false"]
        assert preferences.call_count == 2


def test_processes_episode_five_through_nine_once(monkeypatch, user, show_episodes):
    show, episodes = show_episodes
    subscription = ShowAutomaticDownload.objects.create(
        user=user,
        show=show,
        start_season_number=1,
        start_episode_number=5,
    )
    looked_up = []
    sent = []

    def streams(_media_type, _imdb_id, *, season_number, episode_number, query):
        looked_up.append((season_number, episode_number, query))
        return [
            Stream(
                title=f"Automatic Show S01E{episode_number:02d} 1080p",
                info_hash=f"{episode_number:040x}",
                seeders=episode_number,
                size_mb=1500,
                quality="1080p",
            )
        ]

    monkeypatch.setattr("sources.automatic._BACKEND.streams", streams)
    monkeypatch.setattr("sources.automatic.DownloadClient.add_magnet", lambda _self, uri, **kwargs: sent.append(uri))

    result = process_subscription(subscription, refresh=False)

    assert result.sent == 5
    assert [episode_number for _season, episode_number, _query in looked_up] == [5, 6, 7, 8, 9]
    assert len(sent) == 5
    assert set(
        AutomaticEpisodeDownload.objects.filter(status="sent").values_list(
            "item__episode_number", flat=True
        )
    ) == {5, 6, 7, 8, 9}

    again = process_subscription(subscription, refresh=False)
    assert again.sent == 0
    assert len(sent) == 5

    episodes[9].release_date = timezone.localdate()
    episodes[9].save(update_fields=["release_date"])
    later = process_subscription(subscription, refresh=False)
    assert later.sent == 1
    assert sent[-1].startswith("magnet:?xt=urn:btih:")


def test_up_next_toggle_starts_at_current_episode_and_disables(
    monkeypatch, user, show_episodes
):
    show, _episodes = show_episodes
    client = Client()
    client.force_login(user)
    monkeypatch.setattr(
        "sources.automatic.process_subscription",
        lambda _subscription: ProcessResult(sent=5),
    )
    url = reverse("tracking:up_next_automatic_download", args=[show.pk, 1, 5])

    enabled = client.post(url)

    assert enabled.status_code == 200
    subscription = ShowAutomaticDownload.objects.get(user=user, show=show)
    assert (subscription.start_season_number, subscription.start_episode_number) == (1, 5)
    assert 'aria-pressed="true"' in enabled.content.decode()

    disabled = client.post(url)

    assert disabled.status_code == 200
    assert not ShowAutomaticDownload.objects.filter(user=user, show=show).exists()
    assert 'aria-pressed="false"' in disabled.content.decode()


def test_toggle_can_start_at_first_episode_of_a_new_season(
    monkeypatch, user, show_episodes
):
    show, episodes = show_episodes
    for episode in episodes[4:]:
        Episode.objects.create(
            user=user,
            item=episode,
            status=Status.COMPLETED.value,
            progressed_at=timezone.now(),
        )
    show.raw["seasons"] = [
        {
            "season_number": 2,
            "air_date": timezone.localdate().isoformat(),
            "episode_count": 3,
        }
    ]
    show.save(update_fields=["raw"])
    monkeypatch.setattr(
        "sources.automatic.process_subscription",
        lambda _subscription: ProcessResult(sent=3),
    )
    client = Client()
    client.force_login(user)

    response = client.post(
        reverse("tracking:up_next_automatic_download", args=[show.pk, 2, 1])
    )

    assert response.status_code == 200
    subscription = ShowAutomaticDownload.objects.get(user=user, show=show)
    assert (subscription.start_season_number, subscription.start_episode_number) == (2, 1)
    assert 'aria-pressed="true"' in response.content.decode()
