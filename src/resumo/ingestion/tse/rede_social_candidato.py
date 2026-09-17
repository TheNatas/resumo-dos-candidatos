"""Collector for the TSE ``rede_social_candidato`` bulk product."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Candidacy, CandidateSocialLink
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import download_to_tempfile
from resumo.ingestion.ledger import already_ingested, content_hash, record_ingestion, scoped_key
from resumo.ingestion.tse import ckan, parsing
from resumo.util import clean


def _value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = clean(row.get(name))
        if value:
            return value
    return ""


def _social_row(row: dict[str, str]) -> dict | None:
    sq = _value(row, "SQ_CANDIDATO", "sq_candidato")
    url = _value(row, "DS_URL_REDE_SOCIAL", "URL_REDE_SOCIAL", "DS_URL", "URL")
    if not sq or not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    network = _value(
        row,
        "DS_TIPO_REDE_SOCIAL",
        "TP_REDE_SOCIAL",
        "NM_REDE_SOCIAL",
        "TIPO_REDE_SOCIAL",
    ) or "Rede social"
    return {"sq_candidato": sq, "network": network, "url": url}


class RedeSocialCandidatoCollector(Collector):
    name = "tse_rede_social_candidato"

    def run(
        self,
        session: Session,
        *,
        source: Path | str | None = None,
        year: int | None = None,
        ufs: list[str] | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        year = year or settings.election_year
        uf_scope = tuple(u.upper() for u in ufs) if ufs is not None else settings.uf_list
        tmp: Path | None = None
        if source is not None:
            data_path: Path | str = source
            digest = content_hash(Path(source).read_bytes())
            source_url = str(source)
        else:
            source_url = ckan.resolve_resource_url(
                f"candidatos-{year}",
                "rede_social_candidato",
                fallback=(
                    f"{settings.tse_cdn_base.rstrip('/')}/consulta_cand/"
                    f"rede_social_candidato_{year}.zip"
                ),
            )
            tmp, digest = download_to_tempfile(source_url)
            data_path = tmp

        ledger_url = scoped_key(source_url, uf=",".join(uf_scope))
        try:
            if already_ingested(session, ledger_url, digest):
                return CollectorResult(self.name, "skipped", 0, "unchanged (hash match)")

            known = {
                sq
                for (sq,) in session.execute(
                    select(Candidacy.sq_candidato).where(
                        Candidacy.sg_uf.in_(uf_scope) if uf_scope else True
                    )
                )
            }
            rows = [r for r in (_social_row(row) for row in parsing.iter_records(data_path)) if r]
            rows = [r for r in rows if r["sq_candidato"] in known]
            # The TSE export can repeat the same URL for a candidate. Collapse those
            # rows before insertion because the database intentionally stores one
            # declaration per candidate/network/URL.
            rows = list(
                {
                    (row["sq_candidato"], row["network"], row["url"]): row for row in rows
                }.values()
            )
            # Replace the candidate's current declaration set so removed links do not linger.
            for sq in {r["sq_candidato"] for r in rows}:
                session.execute(delete(CandidateSocialLink).where(CandidateSocialLink.sq_candidato == sq))
            session.add_all(CandidateSocialLink(**row) for row in rows)
            record_ingestion(
                session, collector_name=self.name, source_url=ledger_url, digest=digest, row_count=len(rows)
            )
            return CollectorResult(self.name, "ingested", len(rows), f"uf={','.join(uf_scope) or 'ALL'}")
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)