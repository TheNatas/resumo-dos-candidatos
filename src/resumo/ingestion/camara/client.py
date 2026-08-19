"""Câmara dos Deputados client — API v2 **and** the institutional portal.

Handles the `{dados, links}` envelope, `rel=next` pagination, polite throttling and
basic retry/backoff on 429/5xx. Sync httpx.

Two hosts, one client: `dadosabertos.camara.leg.br` for the JSON API, and
`camara.leg.br` for the pages the API does not cover — today only the relatório de
presença em plenário, which is the sole official source of *dias faltados*.
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

# Transitórios: vale repetir. Qualquer outro status é resposta definitiva da
# fonte, e repetir só atrasa a coleta.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class CamaraClient:
    def __init__(self, client: httpx.Client | None = None, max_retries: int = 4):
        settings = get_settings()
        self._base = settings.camara_api_base.rstrip("/")
        self._portal = settings.camara_portal_base.rstrip("/")
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

    def _fetch(
        self, url: str, params: dict | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        for attempt in range(self._max_retries):
            try:
                resp = self._client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                # Só status transitório merece nova tentativa. Um 404/400 é resposta
                # definitiva da fonte: repetir três vezes com backoff só gasta 7 s por
                # recurso inexistente e atrasa a coleta inteira.
                if status not in _RETRYABLE or attempt == self._max_retries - 1:
                    raise
                backoff = 2**attempt
                logger.warning("Câmara %s on %s — retry in %ss", status, url, backoff)
                time.sleep(backoff)
        raise RuntimeError("unreachable")

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        return self._fetch(url, params).json()

    def get_portal_html(self, path: str, params: dict | None = None) -> str:
        """GET a page from ``camara.leg.br`` (the institutional portal) as HTML.

        Separate from :meth:`get` because it is a different host with a different
        contract: the portal is server-rendered HTML and answers **500** — not 404 —
        for a deputy id that does not exist. It is here, on the same client, so the
        portal inherits the API's retry/backoff and the project's User-Agent instead
        of growing a second transport.

        Needed because ``dadosabertos`` publishes no frequency resource at all; the
        official attendance report exists only on the portal.
        """
        url = path if path.startswith("http") else f"{self._portal}/{path.lstrip('/')}"
        return self._fetch(url, params, {"Accept": "text/html"}).text

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
