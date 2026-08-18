"""ALESC HTTP client — three unrelated hosts, no API, no documentation.

Source of truth (all verified live 2026-08-18):

* ``alesc_site_base``          — WordPress institutional site. Only useful endpoint is
  the **undocumented** Ajax Load More plugin route ``/wp-admin/admin-ajax.php``.
* ``alesc_elegis_base``        — e-Legis (processo legislativo): sessions, ordem do
  dia, extratos de votação, presença, proposições. Server-rendered HTML + htmx.
* ``alesc_transparencia_base`` — bulk CSV (semicolon-delimited, UTF-8 **with BOM**).

Politeness: e-Legis has no ``robots.txt`` (``/robots.txt`` 404s, meta is
``index, follow``) and transparência allows ``/``. No rate limiting was observed, but
a full session crawl is ~1,220 requests, so :func:`~resumo.ingestion.http.throttle`
is applied **inside** :meth:`AlescClient._request` — the fan-out here is four levels
deep (index -> session -> item -> extrato) and centralizing the delay is the only way
to guarantee no code path skips it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from resumo.config import get_settings
from resumo.ingestion.alesc.parsing import is_electoral_blackout
from resumo.ingestion.http import make_client, throttle

logger = logging.getLogger("resumo.ingestion.alesc")

_RETRYABLE = (429, 500, 502, 503, 504)


class AlescBlackoutError(RuntimeError):
    """The institutional site served the *Período Eleitoral* notice instead of content.

    🚨 During the electoral blackout ``{alesc_site_base}/deputado/{slug}/`` 302s to
    ``/aviso-periodo-eleitoral/``. Nothing in this package may depend on profile
    pages; callers are expected to catch this and skip, never to retry.
    """


class AlescClient:
    def __init__(self, client: httpx.Client | None = None, max_retries: int = 4):
        settings = get_settings()
        self.site_base = settings.alesc_site_base.rstrip("/")
        self.elegis_base = settings.alesc_elegis_base.rstrip("/")
        self.transparencia_base = settings.alesc_transparencia_base.rstrip("/")
        self._client = client or make_client()
        self._owns = client is None
        self._max_retries = max_retries

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> AlescClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── transport ────────────────────────────────────────────────────────────
    def _request(
        self,
        url: str,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_retries):
            throttle()
            try:
                resp = self._client.get(url, params=params, headers=headers)
                if resp.status_code in _RETRYABLE:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
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
                logger.warning("ALESC %s on %s — retry in %ss", status, url, backoff)
                time.sleep(backoff)
        raise RuntimeError("unreachable")

    @staticmethod
    def _abs(base: str, path: str) -> str:
        return path if path.startswith("http") else f"{base}/{path.lstrip('/')}"

    # ── e-Legis ──────────────────────────────────────────────────────────────
    def get_elegis(self, path: str, params: dict | None = None, *, htmx: bool = False) -> str:
        """GET an e-Legis page (or htmx fragment) and return its HTML.

        `htmx=True` sends ``X-Requested-With: XMLHttpRequest`` — ``/extrato-votacao/*``
        answers with the full page chrome (or nothing useful) without it.
        """
        headers = {"X-Requested-With": "XMLHttpRequest"} if htmx else None
        return self._request(self._abs(self.elegis_base, path), params, headers).text

    # ── Institutional site (WordPress) ───────────────────────────────────────
    def get_site_json(self, path: str, params: dict | None = None) -> Any:
        resp = self._request(
            self._abs(self.site_base, path), params, {"Accept": "application/json"}
        )
        if is_electoral_blackout(resp.text, str(resp.url)):
            raise AlescBlackoutError(f"electoral blackout notice served for {resp.url}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AlescBlackoutError(
                f"{resp.url} did not return JSON (content-type="
                f"{resp.headers.get('content-type')!r})"
            ) from exc

    def get_site_html(self, path: str, params: dict | None = None) -> str:
        """GET an institutional-site page. Raises :class:`AlescBlackoutError` when the
        *Período Eleitoral* notice is served instead (profile pages, currently all)."""
        resp = self._request(self._abs(self.site_base, path), params)
        if is_electoral_blackout(resp.text, str(resp.url)):
            raise AlescBlackoutError(f"electoral blackout notice served for {resp.url}")
        return resp.text

    # ── Transparência (bulk CSV) ─────────────────────────────────────────────
    def get_transparencia_csv(self, path: str) -> tuple[str, bytes]:
        """GET a transparência CSV; return ``(decoded_text, raw_bytes)``.

        The files are ``text/csv; charset=UTF-8`` **with a BOM** — decoded as
        ``utf-8-sig`` so the first column header is ``Verba`` and not ``\\ufeffVerba``.
        Raw bytes come back too because the ledger hashes the artifact, not the parse.
        """
        resp = self._request(self._abs(self.transparencia_base, path))
        raw = resp.content
        return raw.decode("utf-8-sig", errors="replace"), raw
