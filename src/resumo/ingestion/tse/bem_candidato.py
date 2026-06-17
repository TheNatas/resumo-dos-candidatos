"""Collector: TSE bem_candidato -> CandidateAsset (declared assets / bens)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Candidacy, CandidateAsset
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import download_to_tempfile
from resumo.ingestion.ledger import already_ingested, content_hash, record_ingestion, upsert
from resumo.ingestion.tse import ckan, parsing
from resumo.util import clean, parse_date, parse_decimal, parse_int


def _asset_row(r: dict[str, str]) -> dict | None:
    sq = clean(r.get("SQ_CANDIDATO"))
    ordem = parse_int(r.get("NR_ORDEM_BEM_CANDIDATO"))
    if not sq or ordem is None:
        return None
    return {
        "sq_candidato": sq,
        "nr_ordem_bem": ordem,
        "ds_tipo_bem": clean(r.get("DS_TIPO_BEM_CANDIDATO")),
        "ds_bem": clean(r.get("DS_BEM_CANDIDATO")),
        "vr_bem": parse_decimal(r.get("VR_BEM_CANDIDATO")),
        "dt_ultima_atualizacao": parse_date(r.get("DT_ULTIMA_ATUALIZACAO")),
    }


class BemCandidatoCollector(Collector):
    name = "tse_bem_candidato"

    def run(
        self, session: Session, *, source: Path | str | None = None, year: int | None = None, **_
    ) -> CollectorResult:
        year = year or get_settings().election_year
        tmp: Path | None = None
        if source is not None:
            data_path: Path | str = source
            digest = content_hash(Path(source).read_bytes())
            source_url = str(source)
        else:
            source_url = ckan.resolve_resource_url(
                f"candidatos-{year}", "bem_candidato", fallback=ckan.cdn_url("bem_candidato", year)
            )
            tmp, digest = download_to_tempfile(source_url)
            data_path = tmp

        try:
            if already_ingested(session, source_url, digest):
                return CollectorResult(self.name, "skipped", 0, "unchanged (hash match)")

            rows = [a for a in (_asset_row(r) for r in parsing.iter_records(data_path)) if a]
            # FK integrity: only keep assets whose candidacy was already ingested.
            known = {sq for (sq,) in session.execute(select(Candidacy.sq_candidato))}
            rows = [a for a in rows if a["sq_candidato"] in known]

            n = upsert(
                session,
                CandidateAsset,
                rows,
                index_elements=["sq_candidato", "nr_ordem_bem"],
            )
            record_ingestion(
                session, collector_name=self.name, source_url=source_url, digest=digest, row_count=n
            )
            return CollectorResult(self.name, "ingested", n)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
