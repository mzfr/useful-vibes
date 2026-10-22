"""Registry: source/media-type routing + lazy instantiation."""
from __future__ import annotations

import pytest
from django.test import override_settings

from providers import registry
from providers.base import NotSupported


@pytest.fixture(autouse=True)
def reset_registry():
    registry.reset_cache()
    yield
    registry.reset_cache()


@override_settings(TMDB_API_KEY="fake-key")
def test_get_tmdb():
    p = registry.get("tmdb")
    assert p.source == "tmdb"


def test_get_openlibrary():
    p = registry.get("openlibrary")
    assert p.source == "openlibrary"


def test_get_unknown_source():
    with pytest.raises(NotSupported):
        registry.get("hardcover")


@override_settings(TMDB_API_KEY="fake-key")
def test_for_media_type_routes_correctly():
    assert registry.for_media_type("movie").source == "tmdb"
    assert registry.for_media_type("tv").source == "tmdb"
    assert registry.for_media_type("season").source == "tmdb"
    assert registry.for_media_type("episode").source == "tmdb"
    assert registry.for_media_type("book").source == "openlibrary"


def test_for_media_type_rejects_unknown():
    with pytest.raises(NotSupported):
        registry.for_media_type("anime")


def test_all_sources_listed():
    assert set(registry.all_sources()) == {"tmdb", "openlibrary"}
