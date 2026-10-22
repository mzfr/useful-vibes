"""
Tiny shared httpx client. One per provider (configured with its base URL + auth).
Centralizes timeouts, retries, and error mapping so individual provider modules
stay focused on payload shaping.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import ProviderError

log = logging.getLogger(__name__)


class HTTPClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers or {},
            params=params or {},
            timeout=timeout,
            # Provider endpoints are fixed; redirects can escape base_url and
            # turn untrusted provider paths into SSRF.
            follow_redirects=False,
        )

    def get_json(self, path: str, **params: Any) -> dict[str, Any]:
        # Drop None values so callers can pass optional params unconditionally.
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._client.get(path, params=clean)
        except httpx.HTTPError as exc:
            raise ProviderError(f"HTTP error calling {path!r}: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"{path!r} returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{path!r} returned non-JSON body") from exc

    def close(self) -> None:
        self._client.close()
