from __future__ import annotations

import pytest
from django.test import override_settings

from core.models import AppSettings
from core.settings_store import history_public_url, history_sync_interval, tmdb_api_key

pytestmark = pytest.mark.django_db


@override_settings(TMDB_API_KEY="environment-key")
def test_tmdb_key_falls_back_to_environment():
    assert tmdb_api_key() == "environment-key"


@override_settings(INTAKE_HISTORY_PUBLIC_URL="https://environment.example/")
def test_history_public_url_falls_back_to_environment():
    assert history_public_url() == "https://environment.example"


@override_settings(INTAKE_HISTORY_PUBLIC_URL="https://environment.example")
def test_history_public_url_database_value_takes_precedence():
    app_settings = AppSettings.load()
    app_settings.history_public_url = "https://history.example/"
    app_settings.save()

    assert history_public_url() == "https://history.example"


def test_history_sync_interval_defaults_to_twelve_hours():
    assert history_sync_interval() == (12, "hours", 43200)


@override_settings(TMDB_API_KEY="environment-key")
def test_encrypted_database_key_takes_precedence():
    app_settings = AppSettings.load()
    app_settings.set_tmdb_api_key("database-key")
    app_settings.save()

    assert tmdb_api_key() == "database-key"


@override_settings(SECRET_KEY="different-key", TMDB_API_KEY="environment-key")
def test_unreadable_database_key_falls_back_to_environment():
    app_settings = AppSettings.load()
    with override_settings(SECRET_KEY="original-key"):
        app_settings.set_tmdb_api_key("database-key")
        app_settings.save()

    assert tmdb_api_key() == "environment-key"
