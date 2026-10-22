"""Playwright keyboard-nav test for the episode grid.

Runs against Django's `live_server` so the static assets (`episode_grid.js`,
HTMX, Alpine, Tailwind output) are served the same way they are in dev/prod.

This test is skipped automatically if Playwright's browser binaries aren't
installed — run `uv run playwright install chromium` first.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
import respx
from django.contrib.auth import get_user_model
from httpx import Response

from core.enums import MediaType, Status
from core.models import Item
from tracking.models import Episode

# Skip the whole module if playwright isn't usable.
pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.e2e,
]


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            br = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not installed: {exc}")
        yield br
        br.close()


def _build_show():
    show = Item.objects.create(
        source="tmdb", media_type=MediaType.TV.value, media_id="42", title="Test Show"
    )
    season = Item.objects.create(
        source="tmdb", media_type=MediaType.SEASON.value, media_id="42",
        season_number=1, parent=show, title="Season 1",
    )
    today = date.today()
    for n in range(1, 5):
        Item.objects.create(
            source="tmdb", media_type=MediaType.EPISODE.value, media_id="42",
            season_number=1, episode_number=n, parent=season, title=f"Episode {n}",
            release_date=today - timedelta(days=10 - n),
        )
    return show, season


def _login(page, live_server_url: str) -> None:
    page.goto(f"{live_server_url}/login")
    page.fill('input[name="username"]', "kbuser")
    page.fill('input[name="password"]', "kbpass1234")
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(r"/$"))


@respx.mock
def test_keyboard_toggle_marks_focused_episode_watched(live_server, browser, settings):
    settings.TMDB_API_KEY = "fake"

    User = get_user_model()
    user = User.objects.create_user(username="kbuser", password="kbpass1234")
    show, season = _build_show()

    # Mock the TMDB calls the season view makes on render.
    respx.get("https://api.themoviedb.org/3/tv/42").mock(
        return_value=Response(200, json={
            "id": 42, "name": "Test Show", "overview": "",
            "first_air_date": "2020-01-01",
            "number_of_episodes": 4,
            "credits": {"cast": [], "crew": []},
            "external_ids": {},
        })
    )
    respx.get("https://api.themoviedb.org/3/tv/42/season/1").mock(
        return_value=Response(200, json={
            "id": 1, "name": "Season 1", "overview": "", "air_date": "2020-01-01",
            "episodes": [
                {"episode_number": n, "name": f"Episode {n}", "overview": "",
                 "air_date": "2020-01-01", "runtime": 30}
                for n in range(1, 5)
            ],
        })
    )

    context = browser.new_context()
    page = context.new_page()
    try:
        _login(page, live_server.url)
        page.goto(f"{live_server.url}/items/tmdb/tv/42/season/1/")
        page.wait_for_selector("#episode-grid .episode-cell")

        # First cell should already be focused on load.
        focused = page.evaluate("document.activeElement?.dataset?.episode")
        assert focused == "1"

        # Right twice → focus is on episode 3.
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowRight")
        focused = page.evaluate("document.activeElement?.dataset?.episode")
        assert focused == "3"

        # x toggles watched on the focused episode.
        page.keyboard.press("x")
        # Wait until the grid swap finishes and the cell shows ✓ watched.
        page.wait_for_function(
            "() => document.querySelector("
            "  '.episode-cell[data-episode=\"3\"] .text-success'"
            ") !== null"
        )
    finally:
        context.close()

    # Server-side: ep 3 has a Completed tracking row.
    ep3 = Item.objects.get(parent=season, episode_number=3)
    row = Episode.objects.get(user=user, item=ep3)
    assert row.status == Status.COMPLETED.value
