"""Thin HTTP helpers: a configured client, polite throttling, and a streaming
download that hashes while writing to a temp file (TSE zips can be hundreds of MB)."""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

import httpx

from resumo.config import get_settings


def make_client(**kwargs) -> httpx.Client:
    s = get_settings()
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
