"""Collector: CGU `EmendasParlamentares.zip` -> BudgetAmendment.

Source of truth (no auth, no API key, refreshed monthly):
``https://dadosabertos-download.cgu.gov.br/PortalDaTransparencia/saida/
emendas-parlamentares/EmendasParlamentares.zip``

**Why the bulk file and not the REST API.** The Portal da Transparência endpoint
``api.portaldatransparencia.gov.br/api-de-dados/emendas`` would require a
``chave-api-dados`` header, is capped at 15 rows/page with no UF filter, and is
rate-limited to 400 req/min. The bulk file is *strictly richer*: it adds ``Código
Município IBGE``, ``Município``, ``UF``, ``Região``, ``Programa``, ``Ação`` and
``Plano Orçamentário``, none of which the API returns. Staying on the bulk file is
what keeps this whole project no-auth.

**Two things this data does not say, and must never be made to say:**

1. There is **no "valor autorizado"/dotação column anywhere in this source**.
   ``valor_empenhado`` is the best available proxy for "how much the emenda
   committed" and has to be labelled as such wherever it surfaces.
2. The beneficiary municipality is mostly unresolved — for SC/2025 individual
   emendas only ~22 of 179 rows carry an IBGE municipality code; the rest say
   "MÚLTIPLO" or "SANTA CATARINA (UF)". Those are stored as *no municipality*
   rather than as a municipality named "Múltiplo".

Attribution stops at the amendment type: only the two **individual** modalities
(RP6) name a single legislator. Bancada (RP7), comissão (RP9) and relator (RP8)
are collective; the author bridge refuses to link them by construction. See
:mod:`resumo.ingestion.emendas.author_bridge` for the author->mandate edge.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import BudgetAmendment
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.emendas import parsing
from resumo.ingestion.http import download_to_tempfile
from resumo.ingestion.ledger import (
    already_ingested,
    content_hash,
    record_ingestion,
    scoped_key,
    upsert,
)

logger = logging.getLogger("resumo.ingestion.emendas")

_UPDATE_COLUMNS = [
    "codigo_emenda", "ano", "tipo_emenda_raw", "tipo", "siop_author_code",
    "author_name_raw", "author_name_normalizado", "codigo_municipio_ibge", "municipio",
    "codigo_uf_ibge", "uf", "regiao", "nome_funcao", "nome_subfuncao", "nome_programa",
    "nome_acao", "valor_empenhado", "valor_liquidado", "valor_pago",
    "valor_resto_inscrito", "valor_resto_cancelado", "valor_resto_pago",
]


class EmendasParlamentaresCollector(Collector):
    name = "emendas_parlamentares"

    def run(
        self,
        session: Session,
        *,
        source: Path | str | None = None,
        ufs: list[str] | None = None,
        anos: list[int] | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        # Scope defaults come from config (SC); the kwargs let one run widen or
        # narrow without env edits. `anos` is optional — the file spans 2014-2026.
        scope = parsing.uf_scope(ufs if ufs is not None else settings.uf_list)
        ano_scope = frozenset(anos) if anos else frozenset()

        tmp: Path | None = None
        if source is not None:
            data_path: Path | str = source
            digest = content_hash(Path(source).read_bytes())
            source_url = str(source)
        else:
            source_url = settings.emendas_bulk_url
            tmp, digest = download_to_tempfile(source_url)
            data_path = tmp

        ledger_url = scoped_key(
            source_url,
            uf=",".join(scope.siglas),
            ano=",".join(str(a) for a in sorted(ano_scope)),
        )

        try:
            if already_ingested(session, ledger_url, digest):
                return CollectorResult(self.name, "skipped", 0, "unchanged (hash match)")

            rows: dict[str, dict] = {}
            seen = 0
            out_of_uf = 0
            uf_code_only = 0
            out_of_ano = 0
            unidentifiable = 0
            tipos: Counter[str] = Counter()

            for record in parsing.iter_records(data_path):
                seen += 1
                verdict = parsing.uf_verdict(record, scope)
                if verdict != "in_scope":
                    out_of_uf += 1
                    if verdict == "code_only":
                        uf_code_only += 1
                    continue
                row = parsing.amendment_row(record)
                if row is None:
                    unidentifiable += 1
                    continue
                if ano_scope and row["ano"] not in ano_scope:
                    out_of_ano += 1
                    continue
                tipos[row["tipo"].value] += 1
                rows[row["row_hash"]] = row

            if uf_code_only:
                # The UF column carries the full state NAME; if rows start matching
                # only by IBGE code, the source changed format and the name-based
                # filter is about to silently return nothing. Say so, loudly.
                logger.error(
                    "emendas: %d rows matched the target UF by 'Código UF IBGE' but NOT by "
                    "the 'UF' name — the source's UF format may have changed",
                    uf_code_only,
                )

            n = upsert(
                session,
                BudgetAmendment,
                list(rows.values()),
                index_elements=["row_hash"],
                update_columns=_UPDATE_COLUMNS,
            )
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=ledger_url,
                digest=digest,
                row_count=n,
            )

            uf_label = ",".join(scope.siglas) or "ALL"
            ano_label = ",".join(str(a) for a in sorted(ano_scope)) or "ALL"
            parts = [
                f"uf={uf_label} ano={ano_label}",
                f"{seen} rows read",
                " · ".join(f"{k}={v}" for k, v in sorted(tipos.items())) or "no rows in scope",
            ]
            if out_of_uf:
                parts.append(f"{out_of_uf} out of UF scope")
            if unidentifiable:
                parts.append(f"{unidentifiable} without código da emenda (dropped)")
            if out_of_ano:
                parts.append(f"{out_of_ano} out of year scope")
            if uf_code_only:
                parts.append(f"⚠ {uf_code_only} matched UF by IBGE code only")
            return CollectorResult(self.name, "ingested", n, " · ".join(parts))
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
