"""Câmara dos Deputados API v2 client.

Handles the `{dados, links}` envelope, `rel=next` pagination, polite throttling and
basic retry/backoff on 429/5xx. Sync httpx.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from resumo.config import get_settings
from resumo.ingestion.http import make_client, throttle

logger = logging.getLogger("resumo.ingestion.camara")


class CamaraClient:
    def __init__(self, client: httpx.Client | None = None, max_retries: int = 4):
        self._base = get_settings().camara_api_base.rstrip("/")
        self._client = client or make_client(headers={"Accept": "application/json"})
        self._owns = client is None
        self._max_retries = max_retries

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> CamaraClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, url: str, params: dict | None = None) -> dict[str, Any]:
        for attempt in range(self._max_retries):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if attempt == self._max_retries - 1:
                    raise
                backoff = 2**attempt
                logger.warning("Câmara %s on %s — retry in %ss", status, url, backoff)
                time.sleep(backoff)
        raise RuntimeError("unreachable")

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        return self._request(url, params)

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict[str, Any]]:
        """Yield every item in `dados` across all pages (following rel=next)."""
        params = dict(params or {})
        params.setdefault("itens", 100)
        payload = self.get(path, params)
        while True:
            yield from payload.get("dados", [])
            nxt = next((lk["href"] for lk in payload.get("links", []) if lk.get("rel") == "next"), None)
            if not nxt:
                return
            throttle()
            payload = self.get(nxt)
