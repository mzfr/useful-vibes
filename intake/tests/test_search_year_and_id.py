"""Home search: default 2000+ year filter and pasted-TMDB-ID lookup."""
import pytest
from django.urls import reverse

from core.enums import MediaType
from core.models import Item

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user(username="yearsearch", password="pw")


@pytest.fixture
def old_movie(db):
    return Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="123",
        title="Searchi", release_date="1954-01-01",
    )


@pytest.fixture
def new_movie(db):
    return Item.objects.create(
        source="tmdb", media_type=MediaType.MOVIE.value, media_id="456",
        title="Searchi", release_date="2023-01-01",
    )


def _login(client, user):
    client.force_login(user)


# --------------------------------------------------------------- year filter


def test_year_filter_hides_pre_2000_by_default(client, user, old_movie, new_movie):
    _login(client, user)

    body = client.get("/", {"q": "Searchi"}).content.decode()

    assert "Searchi" in body
    assert old_movie.title not in body or body.count(">Searchi<") == 1
    assert "older match hidden by the 2000+ filter" in body


def test_year_filter_off_shows_old_results(client, user, old_movie, new_movie):
    _login(client, user)

    body = client.get("/", {"q": "Searchi", "year_filter": "off"}).content.decode()

    assert body.count(">Searchi<") == 2
    assert "hidden by the 2000+ filter" not in body


def test_year_filter_checkbox_state_survives_htmx_swap(client, user):
    """The checkbox lives in the form; its checked state follows the request."""
    _login(client, user)

    on_body = client.get("/", {"q": "abc"}).content.decode()
    off_body = client.get(
        "/", {"q": "abc", "year_filter": "off"}
    ).content.decode()

    assert "checked" in on_body
    assert "checked" not in off_body


def test_year_filter_propagates_to_remote_search(client, user):
    _login(client, user)

    body = client.get("/", {"q": "Untracked Oldie"}).content.decode()

    assert "year_filter=off" not in body
    body_off = client.get(
        "/", {"q": "Untracked Oldie", "year_filter": "off"}
    ).content.decode()

    assert "year_filter=off" in body_off


# ------------------------------------------------------------- TMDB-ID paste


def test_local_numeric_id_match_suppresses_remote_search(client, user, old_movie):
    """An exact local media_id match scores 1000, so TMDB is never queried."""
    _login(client, user)

    body = client.get("/", {"q": "123", "year_filter": "off"}).content.decode()

    assert reverse("tracking:details", args=[123]) in body
    assert "Checking TMDB" not in body


def test_local_numeric_id_match_keeps_remote_search_suppressed(client, user, new_movie):
    _login(client, user)

    body = client.get("/", {"q": "456"}).content.decode()

    assert ">Searchi<" in body
    assert "Checking TMDB" not in body


def test_remote_id_lookup_returns_tmdb_details(client, user, fake_tmdb):
    _login(client, user)

    body = client.get(
        reverse("core:remote_search"), {"q": "9001", "type": "movie"}
    ).content.decode()

    # fake_tmdb details title is "<type>-<id>".
    assert "movie-9001" in body
    assert "TMDB ID 9001" in body


def test_remote_id_lookup_skips_hidden_counts_on_dedup(client, user, fake_tmdb):
    """The text search duplicate of the ID match is dropped, not shown twice."""
    _login(client, user)

    body = client.get(
        reverse("core:remote_search"), {"q": "9001", "type": "movie"}
    ).content.decode()

    # One card from the ID hit; the text-search duplicate (same id) is removed.
    assert body.count(">movie-9001<") == 1
    assert "9001 (movie)" not in body


def test_remote_id_lookup_unknown_id_still_runs_text_search(
    client, user, fake_tmdb, monkeypatch
):
    from providers.base import ProviderError

    def not_found(media_type, media_id):
        raise ProviderError("'/movie/x' returned 404")

    monkeypatch.setattr(fake_tmdb, "details", not_found)
    _login(client, user)

    body = client.get(
        reverse("core:remote_search"), {"q": "777777", "type": "movie"}
    ).content.decode()

    # Unresolvable ID falls back to the ordinary text search results.
    assert "777777 (movie)" in body
    assert "TMDB ID" not in body
