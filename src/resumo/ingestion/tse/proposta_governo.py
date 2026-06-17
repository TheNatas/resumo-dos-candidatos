"""Collector: TSE proposta_governo PDFs -> GovernmentProposal.

Majoritarian offices only. The bulk per-UF zips have no machine-readable manifest,
so PDF->candidate mapping is best-effort: we match a long digit-run in the PDF path
against a known SQ_CANDIDATO. Unmatched PDFs are stored as orphans and logged (this
fragility is a documented risk; DivulgaCand's per-candidate path is the fallback).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Candidacy, GovernmentProposal
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import download_to_tempfile
from resumo.ingestion.ledger import already_ingested, content_hash, record_ingestion, upsert
from resumo.ingestion.tse import ckan, parsing

logger = logging.getLogger("resumo.ingestion.tse")
_DIGIT_RUN = re.compile(r"\d{10,}")


def _match_sq(filename: str, known: set[str]) -> str | None:
    for run in _DIGIT_RUN.findall(filename):
        if run in known:
            return run
    return None


class PropostaGovernoCollector(Collector):
    name = "tse_proposta_governo"

    def run(
        self,
        session: Session,
        *,
        source: Path | str | None = None,
        year: int | None = None,
        uf: str | None = None,
        **_,
    ) -> CollectorResult:
        year = year or get_settings().election_year
        tmp: Path | None = None
        if source is not None:
            data_path: Path | str = source
            digest = content_hash(Path(source).read_bytes())
            source_url = str(source)
        else:
            if not uf:
                raise ValueError("proposta_governo needs a `uf` (per-UF zip) when downloading")
            source_url = ckan.cdn_url("proposta_governo", year, uf)
            tmp, digest = download_to_tempfile(source_url)
            data_path = tmp

        try:
            if already_ingested(session, source_url, digest):
                return CollectorResult(self.name, "skipped", 0, "unchanged (hash match)")

            known = {sq for (sq,) in session.execute(select(Candidacy.sq_candidato))}
            storage = get_settings().storage_path() / "proposta" / str(year)
            storage.mkdir(parents=True, exist_ok=True)

            rows: list[dict] = []
            orphans = 0
            for member in parsing.list_pdf_members(data_path):
                sq = _match_sq(member, known)
                if not sq:
                    orphans += 1
                    continue
                pdf_bytes = parsing.read_member(data_path, member)
                phash = content_hash(pdf_bytes)
                out = storage / f"{sq}_{phash[:8]}.pdf"
                out.write_bytes(pdf_bytes)
                rows.append(
                    {
                        "sq_candidato": sq,
                        "source": "tse_bulk_pdf",
                        "storage_path": str(out),
                        "original_filename": Path(member).name,
                        "content_hash": phash,
                    }
                )

            n = upsert(
                session,
                GovernmentProposal,
                rows,
                index_elements=["sq_candidato", "content_hash"],
            )
            record_ingestion(
                session, collector_name=self.name, source_url=source_url, digest=digest, row_count=n
            )
            detail = f"{orphans} unmapped PDFs" if orphans else None
            return CollectorResult(self.name, "ingested", n, detail)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
