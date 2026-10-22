"""Offline Trakt export (ZIP/folder) importer — the only supported Trakt path."""
from __future__ import annotations

import json
import zipfile

import pytest
from django.contrib.auth import get_user_model

from imports.trakt_export import extract_export, run_offline_import
from tracking.models import TV, Episode, Movie

pytestmark = pytest.mark.django_db


def _write(folder, name, payload):
    (folder / name).write_text(json.dumps(payload), encoding="utf-8")


def _movie_play(tmdb_id, watched_at):
    return {
        "id": f"movie-{tmdb_id}-{watched_at}",
        "type": "movie", "watched_at": watched_at,
        "movie": {"title": "M", "ids": {"tmdb": tmdb_id}},
    }


@pytest.fixture
def export_folder(tmp_path):
    folder = tmp_path / "trakt-export"
    folder.mkdir()
    # History is paginated across files, and the same movie is watched 3x.
    _write(folder, "watched-history-1.json", [
        _movie_play(27205, "2020-01-01T10:00:00.000Z"),
        _movie_play(27205, "2022-06-01T10:00:00.000Z"),
    ])
    _write(folder, "watched-history-2.json", [
        _movie_play(27205, "2024-03-01T10:00:00.000Z"),
        {"id": "episode-1", "type": "episode", "watched_at": "2023-05-05T10:00:00.000Z",
         "show": {"ids": {"tmdb": 1399}}, "episode": {"season": 1, "number": 1}},
    ])
    _write(folder, "ratings-movies.json",
           [{"type": "movie", "rating": 9, "movie": {"ids": {"tmdb": 27205}}}])
    _write(folder, "lists-watchlist.json",
           [{"type": "movie", "movie": {"ids": {"tmdb": 603}}}])
    _write(folder, "lists-lists.json",
           [{"name": "Recs", "ids": {"trakt": 111, "slug": "recs"}}])
    # Real exports name this file lists-list-<trakt_id>-<slug>.json.
    _write(folder, "lists-list-111-recs.json",
           [{"type": "movie", "movie": {"ids": {"tmdb": 603}}}])
    _write(folder, "lists-favorites.json",
           [{"type": "movie", "movie": {"ids": {"tmdb": 27205}}}])
    return folder


@pytest.fixture
def user():
    return get_user_model().objects.create_user("u")


def test_offline_import_covers_every_dataset(user, export_folder, fake_tmdb):
    job = run_offline_import(user, export_folder)

    assert job.state == "done", job.error
    assert job.needs_review == []
    assert job.processed == job.total

    # 3 plays collapse into one row with repeats=2, not three rows.
    movie = Movie.objects.get(user=user, item__media_id="27205")
    assert movie.repeats == 2
    assert movie.score == 9

    # Each play is recoverable as its own completion event, chronologically —
    # this is what stats/heatmaps/CSV export consume.
    from tracking.models import CompletionEvent
    years = sorted(
        ev.completed_at.year
        for ev in CompletionEvent.objects.filter(user=user, item=movie.item)
    )
    assert years == [2020, 2022, 2024]

    assert Episode.objects.filter(user=user).count() == 1
    assert Movie.objects.get(user=user, item__media_id="603").status == "planning"


def test_import_preserves_started_show_when_export_also_watchlists_it(
    user, export_folder, fake_tmdb,
):
    _write(export_folder, "lists-watchlist.json", [
        {"type": "show", "show": {"ids": {"tmdb": 1399}}},
    ])
    # A rating creates a planning show row before the episode history is applied.
    _write(export_folder, "ratings-shows.json", [
        {"type": "show", "rating": 8, "show": {"ids": {"tmdb": 1399}}},
    ])

    job = run_offline_import(user, export_folder)

    assert job.state == "done", job.error
    show = TV.objects.get(user=user, item__media_id="1399")
    assert show.status == "in_progress"
    assert show.score == 8
    assert Episode.objects.filter(user=user, item__media_id="1399", status="completed").count() == 1


def test_import_from_zip(user, export_folder, tmp_path, fake_tmdb):
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in export_folder.iterdir():
            # Nest under a top-level dir, as Trakt's real ZIP does.
            zf.write(path, f"trakt-export/{path.name}")

    folder = extract_export(archive, tmp_path / "out")
    job = run_offline_import(user, folder)

    assert job.state == "done", job.error
    assert Movie.objects.filter(user=user, item__media_id="27205").exists()


def test_zip_path_traversal_is_rejected(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escaped.json", "[]")
    with pytest.raises(ValueError, match="unsafe path"):
        extract_export(archive, tmp_path / "out")


def test_empty_folder_fails_the_job_instead_of_hanging(user, tmp_path):
    job = run_offline_import(user, tmp_path)
    assert job.state == "failed"
    assert "No Trakt export JSON found" in job.error
