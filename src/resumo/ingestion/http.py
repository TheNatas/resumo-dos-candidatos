"""Thin HTTP helpers: a configured client, polite throttling, and a streaming
download that hashes while writing to a temp file (TSE zips can be hundreds of MB)."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from pathlib import Path

import httpx

from resumo.config import get_settings

log = logging.getLogger(__name__)

PLACEHOLDER_CONTACT = "contato@example.org"
_warned_placeholder = False


def _check_contact(user_agent: str) -> None:
    """None of the sources needs an API key, so the User-Agent is the only way an
    operator can be identified — and the only thing that keeps a daily crawl from
    looking anonymous to a WAF. Warn once per process rather than fail: a blocked
    collector at 05:00 is worse than a noisy log line."""
    global _warned_placeholder
    if PLACEHOLDER_CONTACT in user_agent and not _warned_placeholder:
        _warned_placeholder = True
        log.warning(
            "RESUMO_HTTP_USER_AGENT ainda usa o contato de exemplo (%s). Defina um "
            "contato real antes de coletar de um IP compartilhado (CI/servidor): "
            "as fontes não têm chave de API e só podem te identificar por esse header.",
            PLACEHOLDER_CONTACT,
        )


def make_client(**kwargs) -> httpx.Client:
    s = get_settings()
    _check_contact(s.http_user_agent)
    headers = {"User-Agent": s.http_user_agent, "Accept-Encoding": "gzip"}
    headers.update(kwargs.pop("headers", {}))
    return httpx.Client(
        headers=headers,
        timeout=s.http_timeout_seconds,
        follow_redirects=True,
        **kwargs,
    )


def throttle() -> None:
    delay = get_settings().request_delay_seconds
    if delay > 0:
        time.sleep(delay)


def download_to_tempfile(url: str, *, client: httpx.Client | None = None) -> tuple[Path, str]:
    """Stream `url` to a temp file; return (path, sha256). Caller deletes the file."""
    own = client is None
    client = client or make_client()
    digest = hashlib.sha256()
    try:
        fd, name = tempfile.mkstemp(suffix=".download")
        path = Path(name)
        with open(fd, "wb") as out, client.stream("GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                out.write(chunk)
                digest.update(chunk)
        return path, digest.hexdigest()
    finally:
        if own:
            client.close()
