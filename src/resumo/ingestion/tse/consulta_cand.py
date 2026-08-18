"""Collector: TSE consulta_cand -> Candidacy (+ derived Coalition).

This is the TSE-side anchor of the candidate<->mandate link. Person rows are NOT
created here; the resolution pipeline owns identity. We keep cpf_raw/titulo_raw for
(re-)resolution.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from resumo import cargos
from resumo.config import get_settings
from resumo.db.models import Candidacy, Coalition
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import download_to_tempfile
from resumo.ingestion.ledger import (
    already_ingested,
    content_hash,
    record_ingestion,
    scoped_key,
    upsert,
)
from resumo.ingestion.tse import ckan, parsing
from resumo.util import clean, normalize_name, parse_date, parse_int

logger = logging.getLogger("resumo.ingestion.tse")


def _candidacy_row(r: dict[str, str]) -> dict | None:
    sq = clean(r.get("SQ_CANDIDATO"))
    if not sq:
        return None
    cd_cargo = parse_int(r.get("CD_CARGO"))
    return {
        "sq_candidato": sq,
        "ano_eleicao": parse_int(r.get("ANO_ELEICAO")),
        "nr_turno": parse_int(r.get("NR_TURNO")) or 1,
        "cd_eleicao": parse_int(r.get("CD_ELEICAO")),
        "ds_eleicao": clean(r.get("DS_ELEICAO")),
        "cpf_raw": clean(r.get("NR_CPF_CANDIDATO")),
        "titulo_raw": clean(r.get("NR_TITULO_ELEITORAL_CANDIDATO")),
        "nome_candidato": clean(r.get("NM_CANDIDATO")),
        "nome_urna": clean(r.get("NM_URNA_CANDIDATO")),
        "nome_normalizado": normalize_name(r.get("NM_CANDIDATO")),
        "data_nascimento": parse_date(r.get("DT_NASCIMENTO")),
        "cd_cargo": cd_cargo,
        "ds_cargo": clean(r.get("DS_CARGO")),
        "sg_uf": clean(r.get("SG_UF")),
        "sg_ue": clean(r.get("SG_UE")),
        "nm_ue": clean(r.get("NM_UE")),
        "nr_candidato": clean(r.get("NR_CANDIDATO")),
        "sg_partido": clean(r.get("SG_PARTIDO")),
        "nr_partido": parse_int(r.get("NR_PARTIDO")),
        "nm_partido": clean(r.get("NM_PARTIDO")),
        "sq_coligacao": clean(r.get("SQ_COLIGACAO")),
        "ds_situacao_candidatura": clean(r.get("DS_SITUACAO_CANDIDATURA")),
        "ds_detalhe_situacao_cand": clean(r.get("DS_DETALHE_SITUACAO_CAND")),
        "ds_sit_tot_turno": clean(r.get("DS_SIT_TOT_TURNO")),
        "st_reeleicao": clean(r.get("ST_REELEICAO")),
        "is_majoritario": cargos.is_majoritario(cd_cargo),
    }


def _coalition_row(r: dict[str, str]) -> dict | None:
    sq = clean(r.get("SQ_COLIGACAO"))
    if not sq:
        return None
    return {
        "sq_coligacao": sq,
        "nm_coligacao": clean(r.get("NM_COLIGACAO")),
        "ds_composicao_coligacao": clean(r.get("DS_COMPOSICAO_COLIGACAO")),
        "ano_eleicao": parse_int(r.get("ANO_ELEICAO")),
        "sg_uf": clean(r.get("SG_UF")),
        "cd_cargo": parse_int(r.get("CD_CARGO")),
    }


class ConsultaCandCollector(Collector):
    name = "tse_consulta_cand"

    def run(
        self,
        session: Session,
        *,
        source: Path | str | None = None,
        year: int | None = None,
        ufs: list[str] | None = None,
        cargo_codes: list[int] | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        year = year or settings.election_year
        # Scope defaults come from config (SC + the 4 general-election offices); the
        # kwargs exist so a caller can widen or narrow a single run without env edits.
        uf_scope = tuple(u.upper() for u in ufs) if ufs is not None else settings.uf_list
        cargo_scope = frozenset(cargo_codes) if cargo_codes is not None else settings.cargo_set

        # Resolve the artifact (local file for tests/backfill, else download).
        tmp: Path | None = None
        if source is not None:
            data_path: Path | str = source
            digest = content_hash(Path(source).read_bytes())
            source_url = str(source)
        else:
            source_url = ckan.resolve_resource_url(
                f"candidatos-{year}",
                "candidatos_",
                fallback=ckan.cdn_url("consulta_cand", year),
            )
            tmp, digest = download_to_tempfile(source_url)
            data_path = tmp

        ledger_url = scoped_key(
            source_url,
            uf=",".join(uf_scope),
            cargo=",".join(map(str, sorted(cargo_scope))),
        )

        try:
            if already_ingested(session, ledger_url, digest):
                return CollectorResult(self.name, "skipped", 0, "unchanged (hash match)")

            candidacies: dict[str, dict] = {}
            coalitions: dict[str, dict] = {}
            generated_at: str | None = None
            skipped_cargo = 0
            for r in parsing.iter_records(data_path, ufs=uf_scope):
                if generated_at is None:
                    generated_at = clean(r.get("DT_GERACAO"))
                cand = _candidacy_row(r)
                if cand is None:
                    continue
                if cargo_scope and cand["cd_cargo"] not in cargo_scope:
                    skipped_cargo += 1
                    continue
                candidacies[cand["sq_candidato"]] = cand
                # Coalitions are only kept for candidacies we actually ingest, so the
                # table never accumulates FK targets for out-of-scope offices.
                col = _coalition_row(r)
                if col:
                    coalitions[col["sq_coligacao"]] = col

            # Coalitions first (FK target), then candidacies.
            upsert(session, Coalition, list(coalitions.values()), index_elements=["sq_coligacao"])
            n = upsert(session, Candidacy, list(candidacies.values()), index_elements=["sq_candidato"])
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=ledger_url,
                digest=digest,
                row_count=n,
                source_generated_at=generated_at,
            )
            scope = f"uf={','.join(uf_scope) or 'ALL'} cargo={','.join(map(str, sorted(cargo_scope))) or 'ALL'}"
            detail = f"{len(coalitions)} coalitions · {scope}"
            if skipped_cargo:
                detail += f" · {skipped_cargo} rows out of cargo scope"
            return CollectorResult(self.name, "ingested", n, detail)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
