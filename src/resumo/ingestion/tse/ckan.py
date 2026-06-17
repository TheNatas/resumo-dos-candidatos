"""Resolve TSE bulk download URLs.

Primary path: ask the CKAN catalog (`package_show`) for the resource URL — this
survives packaging/path drift between cycles. Fallback: a templated CDN URL built
from the known, stable directory layout.
"""

from __future__ import annotations

import logging

from resumo.config import get_settings
from resumo.ingestion.http import make_client

logger = logging.getLogger("resumo.ingestion.tse")


def cdn_url(produto: str, ano: int, uf: str | None = None) -> str:
    """Templated CDN URL, e.g. .../consulta_cand/consulta_cand_2022.zip or a per-UF
    product like .../proposta_governo/proposta_governo_2022_SP.zip."""
    base = get_settings().tse_cdn_base.rstrip("/")
    stem = f"{produto}_{ano}" + (f"_{uf}" if uf else "")
    return f"{base}/{produto}/{stem}.zip"


def resolve_resource_url(
    package_id: str, name_contains: str, *, fallback: str | None = None
) -> str:
    """Look up a resource URL from CKAN `package_show`; fall back to a templated URL."""
    base = get_settings().tse_ckan_base.rstrip("/")
    try:
        with make_client() as client:
            resp = client.get(f"{base}/package_show", params={"id": package_id})
            resp.raise_for_status()
            resources = resp.json()["result"]["resources"]
        needle = name_contains.lower()
        for res in resources:
            name = (res.get("name") or "").lower()
            url = res.get("url") or ""
            if needle in name and url.lower().endswith(".zip"):
                return url
    except Exception as exc:  # noqa: BLE001 — catalog is best-effort; template is authoritative fallback
        logger.warning("CKAN resolve failed for %s/%s: %s", package_id, name_contains, exc)
    if fallback:
        return fallback
    raise LookupError(f"Could not resolve TSE resource {package_id!r} matching {name_contains!r}")
