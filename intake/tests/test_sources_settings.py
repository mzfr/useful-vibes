"""Settings page: Sources & download client panel."""

from __future__ import annotations

import pytest
import respx
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from httpx import Response

from core.models import AppSettings
from core.settings_store import qbittorrent_config, source_addon_url

pytestmark = pytest.mark.django_db

ADDON = "https://addon.example"


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="u", password="p")


@pytest.fixture
def client(user):
    client = Client()
    client.force_login(user)
    return client


def test_settings_page_renders_sources_panel(client):
    resp = client.get(reverse("imports:index"))
    body = resp.content.decode()
    assert "Sources" in body
    assert "Addon URL" in body
    assert "Client host" in body


def test_defaults_when_nothing_saved(client):
    assert source_addon_url() == "https://torrentio.strem.fun"
    cfg = qbittorrent_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 8080


@respx.mock
def test_save_sources_settings_stores_addon_and_client(client):
    respx.get(f"{ADDON}/addon/catalog/series/tt0844371.json").mock(
        return_value=Response(200, json={"metas": []})
    )
    respx.get("http://qbit.local:8080/api/v2/app/version").mock(
        return_value=Response(200, text="v5.0")
    )

    resp = client.post(
        reverse("imports:save_sources_settings"),
        {
            "source_addon_url": ADDON,
            "qbittorrent_host": "qbit.local",
            "qbittorrent_port": "8080",
            "qbittorrent_username": "admin",
            "qbittorrent_password": "sekret",
        },
    )

    assert resp.status_code == 302
    stored = AppSettings.objects.get()
    assert stored.source_addon_url == ADDON
    assert stored.qbittorrent_host == "qbit.local"
    assert stored.qbittorrent_port == 8080
    # Password is encrypted at rest and decrypts cleanly.
    assert "sekret" not in stored.qbittorrent_password_encrypted
    assert qbittorrent_config()["password"] == "sekret"


def test_save_with_blank_addon_resets_to_default(client):
    s = AppSettings.load()
    s.source_addon_url = ADDON
    s.save()

    resp = client.post(
        reverse("imports:save_sources_settings"),
        {
            "source_addon_url": "",
            "qbittorrent_host": "",
            "qbittorrent_port": "",
            "qbittorrent_username": "",
            "qbittorrent_password": "",
        },
    )

    assert resp.status_code == 302
    assert source_addon_url() == "https://torrentio.strem.fun"


def test_blank_password_keeps_stored_one(client):
    s = AppSettings.load()
    s.qbittorrent_host = "qbit.local"
    s.set_secret("qbittorrent_password", "old-pass")
    s.save()

    client.post(
        reverse("imports:save_sources_settings"),
        {
            "source_addon_url": "",
            "qbittorrent_host": "qbit.local",
            "qbittorrent_port": "8080",
            "qbittorrent_username": "admin",
            "qbittorrent_password": "",
        },
    )

    assert qbittorrent_config()["password"] == "old-pass"


def test_invalid_port_rejected(client):
    resp = client.post(
        reverse("imports:save_sources_settings"),
        {
            "source_addon_url": "",
            "qbittorrent_host": "qbit.local",
            "qbittorrent_port": "99999",
            "qbittorrent_username": "",
            "qbittorrent_password": "",
        },
    )
    assert resp.status_code == 302
    assert not AppSettings.objects.filter(qbittorrent_host="qbit.local").exists()


@respx.mock
def test_unresponsive_addon_rejected(client):
    import httpx as httpx_lib

    respx.get(f"{ADDON}/addon/catalog/series/tt0844371.json").mock(
        side_effect=httpx_lib.ConnectError("nope")
    )

    resp = client.post(
        reverse("imports:save_sources_settings"),
        {
            "source_addon_url": ADDON,
            "qbittorrent_host": "",
            "qbittorrent_port": "",
            "qbittorrent_username": "",
            "qbittorrent_password": "",
        },
    )

    assert resp.status_code == 302
    assert not AppSettings.objects.exclude(source_addon_url="").exists()


@respx.mock
def test_unresponsive_client_saves_with_warning(client):
    import httpx as httpx_lib

    respx.get("http://ghost.local:8080/api/v2/app/version").mock(
        side_effect=httpx_lib.ConnectError("nope")
    )

    resp = client.post(
        reverse("imports:save_sources_settings"),
        {
            "source_addon_url": "",
            "qbittorrent_host": "ghost.local",
            "qbittorrent_port": "8080",
            "qbittorrent_username": "",
            "qbittorrent_password": "",
        },
    )

    assert resp.status_code == 302
    assert AppSettings.objects.filter(qbittorrent_host="ghost.local").exists()
