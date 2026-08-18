"""Senado Federal Dados Abertos client (https://legis.senado.leg.br/dadosabertos).

Two API generations coexist behind the same host and must be handled differently:

* **legacy** ``/senador/*`` — an XML service translated to JSON on demand. Wrapped
  envelopes (``ListaParlamentarLegislatura``, ``DetalheParlamentar``, ...) plus a
  ``Metadados`` block, ``PascalCase`` keys, and *every* value is a string ("22",
  "57"); date params go in as ``AAAAMMDD``.
* **modern** ``/votacao`` and ``/processo`` — a **bare JSON array**, no envelope,
  ``camelCase`` keys, native ints/floats, date params as ``AAAA-MM-DD``.

So :meth:`SenadoClient.get` returns ``Any``: a dict for the legacy tree, a list for
the modern one. There is no pagination anywhere in this API — a response is always
complete, which is exactly why callers must chunk wide queries by year themselves
(the payloads reach several MB).

Two traps this module exists to absorb:

* ``?formato=json`` is silently ignored and yields XML. Only the ``Accept`` header
  switches the representation, so it is pinned on the client.
* several reference endpoints answer 301 to a static file; without redirect
  following httpx hands back an empty body and no error at all. :func:`make_client`
  already sets ``follow_redirects=True`` — do not build a bare ``httpx.Client`` here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from resumo.config import get_settings
from resumo.ingestion.http import make_client

logger = logging.getLogger("resumo.ingestion.senado")

# Transitórios: vale repetir. Qualquer outro status é resposta definitiva da
# fonte, e repetir só atrasa a coleta.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


def _as_list(value: Any) -> list[Any]:
    """Normalize the legacy XML→JSON collapse of single-element arrays.

    The translator emits ``{"Mandato": {...}}`` for one element and
    ``{"Mandato": [{...}, {...}]}`` for many, so any code that assumes a list
    crashes (or silently iterates dict *keys*) exactly on the small result sets a
    state-scoped install produces. Known offenders: ``Parlamentar``, ``Mandato``,
    ``Exercicio``, ``Suplente``, ``Telefone``.
    """
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def dig(payload: Any, *keys: str) -> Any:
    """Walk a legacy envelope path, returning None instead of raising when a level
    is absent — the translator drops empty nodes entirely rather than emitting null."""
    node = payload
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


class SenadoClient:
    def __init__(self, client: httpx.Client | None = None, max_retries: int = 4):
        self._base = get_settings().senado_api_base.rstrip("/")
        # Accept negotiates JSON for both generations; the `formato` query param does not.
        self._client = client or make_client(headers={"Accept": "application/json"})
        self._owns = client is None
        self._max_retries = max_retries

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> SenadoClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, url: str, params: dict | None = None) -> Any:
        for attempt in range(self._max_retries):
            try:
                resp = self._client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                # Só status transitório merece nova tentativa. Um 404/400 é resposta
                # definitiva da fonte: repetir três vezes com backoff só gasta 7 s por
                # recurso inexistente e atrasa a coleta inteira.
                if status not in _RETRYABLE or attempt == self._max_retries - 1:
                    raise
                backoff = 2**attempt
                logger.warning("Senado %s on %s — retry in %ss", status, url, backoff)
                time.sleep(backoff)
        raise RuntimeError("unreachable")

    def get(self, path: str, params: dict | None = None) -> Any:
        """GET a path (or an absolute URL — CEAPS lives on another host)."""
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        return self._request(url, params)
