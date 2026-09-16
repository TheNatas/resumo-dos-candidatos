"""Collector for official party proposals and bounded website discovery."""

from __future__ import annotations

import json
import re
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import PartyProposal
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import download_to_tempfile, make_client, throttle
from resumo.ingestion.ledger import content_hash, upsert

DISCOVERY_TERMS = re.compile(
    r"(programa|proposta|plano|diretriz|elei[cç][aã]o|governo|2026)", re.IGNORECASE
)
MAX_PAGES = 40


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._text: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text)))
            self._href = None


def _same_host(url: str, hosts: set[str]) -> bool:
    return urlparse(url).hostname in hosts


def discover_pdf_urls(domains: list[str], year: int, *, max_pages: int = MAX_PAGES) -> list[str]:
    """Find likely proposal PDFs by crawling only configured official domains."""
    queue: deque[str] = deque()
    hosts: set[str] = set()
    for domain in domains:
        root = domain if domain.startswith("http") else f"https://{domain}"
        root = root.rstrip("/")
        queue.extend([root, f"{root}/programa", f"{root}/eleicoes", f"{root}/eleicoes-{year}"])
        host = urlparse(root).hostname
        if host:
            hosts.add(host)

    visited: set[str] = set()
    pdfs: set[str] = set()
    with make_client() as client:
        while queue and len(visited) < max_pages:
            url = urldefrag(queue.popleft())[0]
            if url in visited or not _same_host(url, hosts):
                continue
            visited.add(url)
            try:
                throttle()
                response = client.get(url)
                response.raise_for_status()
            except Exception:
                continue
            content_type = response.headers.get("content-type", "").lower()
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                if DISCOVERY_TERMS.search(url) or str(year) in url:
                    pdfs.add(url)
                continue
            if "html" not in content_type:
                continue
            parser = _Links()
            parser.feed(response.text)
            for href, text in parser.links:
                target = urldefrag(urljoin(url, href))[0]
                if not _same_host(target, hosts):
                    continue
                label = f"{target} {text}"
                if target.lower().endswith(".pdf") and (DISCOVERY_TERMS.search(label) or str(year) in label):
                    pdfs.add(target)
                elif DISCOVERY_TERMS.search(label) and target not in visited:
                    queue.append(target)
    return sorted(pdfs)


class PartyProposalCollector(Collector):
    name = "official_party_proposals"

    def run(
        self,
        session: Session,
        *,
        manifest: Path | None = None,
        discover: bool = False,
        party: str | None = None,
        domains: list[str] | None = None,
        year: int | None = None,
        uf: str | None = None,
        **_,
    ) -> CollectorResult:
        """Ingest a manifest or discover PDFs from configured official domains."""
        settings = get_settings()
        year = year or settings.election_year
        if discover:
            if not party or not domains:
                raise ValueError("discovery requires --party and at least one --domain")
            entries = [
                {
                    "party_sigla": party,
                    "ano_eleicao": year,
                    "uf": uf,
                    "source_type": "party_website",
                    "source_url": url,
                    "verification_method": "same_domain_pdf_crawl",
                }
                for url in discover_pdf_urls(domains, year)
            ]
            manifest_label = f"domains={','.join(domains)}"
        elif manifest:
            manifest_path = Path(manifest).resolve()
            entries = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_label = str(manifest)
        else:
            raise ValueError("pass --manifest or --discover with --party/--domain")
        rows = []
        for entry in entries:
            local = Path(entry["path"]) if entry.get("path") else None
            if local is not None and not local.is_absolute() and manifest:
                local = Path(manifest).resolve().parent / local
            temp = None
            try:
                if local:
                    data = local.read_bytes()
                else:
                    temp, _ = download_to_tempfile(entry["source_url"])
                    data = temp.read_bytes()
                digest = content_hash(data)
                year = int(entry["ano_eleicao"])
                storage = get_settings().storage_path() / "party-proposta" / str(year)
                storage.mkdir(parents=True, exist_ok=True)
                filename = entry.get("filename") or f"{entry['party_sigla']}-{digest[:8]}.pdf"
                out = storage / filename
                out.write_bytes(data)
                rows.append(
                    {
                        "party_sigla": entry["party_sigla"].upper(),
                        "ano_eleicao": year,
                        "uf": entry.get("uf"),
                        "source_type": entry["source_type"],
                        "source_url": entry["source_url"],
                        "verification_method": entry["verification_method"],
                        "title": entry.get("title"),
                        "storage_path": str(out),
                        "original_filename": filename,
                        "content_hash": digest,
                    }
                )
            finally:
                if temp:
                    temp.unlink(missing_ok=True)
        count = upsert(
            session,
            PartyProposal,
            rows,
            index_elements=["party_sigla", "ano_eleicao", "uf", "content_hash"],
        )
        return CollectorResult(self.name, "ingested", count, manifest_label)