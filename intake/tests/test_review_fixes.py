"""Regression tests for the advisor-review fixes."""
from __future__ import annotations

import io
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.enums import MediaType, Status
from core.models import Item
from imports.csv_movies_shows import _safe_cell
from imports.models import ImportJob
from tracking.bulk import BulkActionError, apply_bulk_action
from tracking.models import Episode, Movie

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def client(user):
    c = Client()
    c.force_login(user)
    return c


# ------------------------------------------------------- CSV formula injection


def test_safe_cell_neutralizes_formulas():
    assert _safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _safe_cell("+SUM(A1)") == "'+SUM(A1)"
    assert _safe_cell("-2+3") == "'-2+3"
    assert _safe_cell("@cmd") == "'@cmd"
    assert _safe_cell("Inception") == "Inception"
    assert _safe_cell(9) == 9
    assert _safe_cell("") == ""


def test_export_neutralizes_malicious_title(user):
    from imports.csv_movies_shows import export_csv
    item = Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="666",
        title="=HYPERLINK(\"http://evil\",\"click\")",
    )
    Movie.objects.create(user=user, item=item, status=Status.COMPLETED.value)
    buf = io.StringIO()
    export_csv(user, buf)
    assert "'=HYPERLINK" in buf.getvalue()


# --------------------------------------------------------------- BOM handling


def test_bom_csv_upload_detected(client, user):
    """A Goodreads CSV with a UTF-8 BOM must still detect its format."""
    csv_body = (
        "\ufeffBook Id,Title,Author,ISBN,ISBN13,My Rating,Exclusive Shelf,Date Read,Date Added\n"
    ).encode("utf-8")
    resp = client.post(
        reverse("imports:upload_csv_books"),
        {"file": io.BytesIO(csv_body)},
    )
    # Redirects to the job page; job should be done (0 rows), NOT failed on
    # format detection.
    assert resp.status_code == 302
    job = ImportJob.objects.get(user=user)
    assert job.state == "done"


def test_non_utf8_upload_rejected_with_400(client):
    resp = client.post(
        reverse("imports:upload_csv_books"),
        {"file": io.BytesIO(b"\xff\xfe\x00bad latin")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------- stale job recovery


def test_fail_stale_running_jobs(user):
    stale = ImportJob.objects.create(user=user, kind="trakt", state="running")
    ImportJob.objects.filter(pk=stale.pk).update(
        started_at=timezone.now() - timedelta(hours=3)
    )
    fresh = ImportJob.objects.create(
        user=user, kind="trakt", state="running", started_at=timezone.now()
    )

    n = ImportJob.fail_stale_running(user)
    assert n == 1
    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.state == "failed"
    assert fresh.state == "running"


# -------------------------------------------------------- shared bulk service


def _season_tree():
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="7", title="Show"
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="7",
        season_number=1, parent=show, title="S1",
    )
    eps = [
        Item.objects.create(
            source="tmdb", media_type=MediaType.EPISODE.value, media_id="7",
            season_number=1, episode_number=n, parent=season, title=f"E{n}",
        )
        for n in (1, 2)
    ]
    return season, eps


def test_bulk_all_sets_progressed_at_on_episodes(user):
    """The drift bug: bulk marking must stamp progressed_at so Up Next
    staleness ordering sees the change."""
    season, eps = _season_tree()
    apply_bulk_action(user=user, season_item=season, action="all")

    for ep in eps:
        row = Episode.objects.get(user=user, item=ep)
        assert row.status == Status.COMPLETED.value
        assert row.repeats == 0
        assert row.progressed_at is not None


def test_bulk_unknown_action_raises(user):
    season, _ = _season_tree()
    with pytest.raises(BulkActionError):
        apply_bulk_action(user=user, season_item=season, action="nonsense")


def test_bulk_range_requires_bounds(user):
    season, _ = _season_tree()
    with pytest.raises(BulkActionError):
        apply_bulk_action(user=user, season_item=season, action="range")


# ------------------------------------------------------ 404 instead of 500


def test_bulk_on_unmaterialized_season_404(client):
    url = reverse("tracking:bulk_season", args=["99999", 1])
    resp = client.post(url, {"action": "all"})
    assert resp.status_code == 404


def test_toggle_on_unmaterialized_season_404(client):
    url = reverse("tracking:toggle_episode", args=["99999", 1, 1])
    resp = client.post(url)
    assert resp.status_code == 404


# ------------------------------------------------- track view input handling


def test_track_unknown_media_type_400(client):
    url = reverse("tracking:track", args=["tmdb", "anime", "123"])
    resp = client.post(url, {"status": "completed"})
    assert resp.status_code == 400


def test_watched_again_unknown_media_type_400(client):
    url = reverse("tracking:watched_again", args=["tmdb", "anime", "123"])
    resp = client.post(url)
    assert resp.status_code == 400


# ----------------------------------------------------- prod SECRET_KEY guard


def test_prod_settings_require_secret_key(monkeypatch):
    """Importing prod settings without INTAKE_SECRET_KEY must raise."""
    import importlib
    import sys

    monkeypatch.delenv("INTAKE_SECRET_KEY", raising=False)
    sys.modules.pop("config.settings.prod", None)
    with pytest.raises(RuntimeError, match="INTAKE_SECRET_KEY"):
        importlib.import_module("config.settings.prod")
    sys.modules.pop("config.settings.prod", None)
