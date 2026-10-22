"""
qBittorrent Web API client — just enough to add a magnet.

Auth flow matches the torrentio script's: try the unauthenticated add first
(works when "Bypass authentication for localhost" is on), fall back to login
on 401/403 and retry once.
"""

from __future__ import annotations

import posixpath
import re

import httpx

from core.settings_store import qbittorrent_config

from .base import StreamProviderError


class DownloadClient:
    """qBittorrent Web API. A different client would be a sibling class behind
    the same add_magnet() interface, picked by a settings field."""

    def __init__(self) -> None:
        config = qbittorrent_config()
        self._base = f"http://{config['host']}:{config['port']}"
        self._username = str(config["username"] or "")
        self._password = str(config["password"] or "")

    def _request(self, client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
        resp = client.request(method, path, timeout=15.0, **kwargs)
        if resp.status_code in (401, 403) and self._username:
            login = client.post(
                "/api/v2/auth/login",
                data={"username": self._username, "password": self._password},
                timeout=10.0,
            )
            if login.status_code not in (200, 204) or "fail" in login.text.strip().lower():
                raise StreamProviderError("Download client rejected the credentials.")
            resp = client.request(method, path, timeout=15.0, **kwargs)
        resp.raise_for_status()
        return resp

    def add_magnet(
        self, magnet: str, *, show_title: str | None = None, season_number: int | None = None
    ) -> None:
        """Add a magnet link; raises StreamProviderError on any failure.

        Login success is judged by HTTP status (200/204), not body text —
        qBittorrent 5.x answers the login endpoint with an empty 204, older
        versions with `Ok.`.
        """
        with httpx.Client(base_url=self._base) as client:
            try:
                data = {"urls": magnet}
                if show_title is not None:
                    preferences = self._request(client, "GET", "/api/v2/app/preferences").json()
                    root = preferences.get("save_path")
                    if not isinstance(root, str) or not root.strip():
                        raise StreamProviderError("Download client has no default save path.")
                    folder = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]', "_", show_title).strip(" .")
                    folder = folder or "Untitled show"
                    path = posixpath.join(root, folder)
                    if season_number is not None:
                        path = posixpath.join(path, f"Season {season_number:02d}")
                    data.update(savepath=path, autoTMM="false")
                resp = self._request(client, "POST", "/api/v2/torrents/add", data=data)
                if resp.text.strip().lower().startswith("fail"):
                    raise StreamProviderError("Download client rejected the torrent.")
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                raise StreamProviderError(f"Download client unreachable: {exc}") from exc

    def ping(self) -> bool:
        """True when the Web API answers (any status). For settings validation."""
        try:
            with httpx.Client(base_url=self._base, timeout=5.0) as client:
                resp = client.get("/api/v2/app/version")
                return resp.status_code < 500
        except httpx.HTTPError:
            return False
