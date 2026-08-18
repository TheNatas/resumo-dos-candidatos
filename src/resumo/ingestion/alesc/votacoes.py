"""Collector: ALESC votações nominais -> Vote.

Source of truth, a four-step chain (there is no votes endpoint):

1. ``{alesc_elegis_base}/sessoes-plenarias?page=N``            — session index
2. ``…/sessoes-plenarias/{hash}/ordem-do-dia``                 — items + vote type
3. items carry an htmx trigger ``hx-get="/extrato-votacao/{hash}"`` when nominal
4. ``…/extrato-votacao/{hash}`` **with ``X-Requested-With: XMLHttpRequest``**
   — an HTML fragment with one Sim/Não badge per deputy

🚨 **THE CEILING: only ~3.6% of ALESC deliberations are nominal.** Measured 44 nominal
out of 1,218 items across 120 sessions. The other ~96% are *votação simbólica*, which
by institutional design records **no individual position at all** — there is nothing
to extract and nothing may be synthesized. Expect roughly **200–250 nominal votes for
the entire 2023–2027 legislature**; a "voting record" for an ALESC deputy is that
thin, and any UI built on it must say so.

🚨 Positions are read off the **badge CSS class** (``text-bg-success`` = Sim,
``text-bg-danger`` = Não), never the label text. No abstention badge was ever observed;
an unknown third class keeps its label and is logged rather than crashing.

🚨 ALESC publishes **no party orientation** for a votação, so `orientacao_partido`
stays ``None`` — party-fidelity analysis is not possible for state deputies.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Vote
from resumo.ingestion.alesc.client import AlescClient
from resumo.ingestion.alesc.common import mandate_index, proposition_id, votacao_id
from resumo.ingestion.alesc.parsing import parse_extrato_votacao, parse_ordem_do_dia
from resumo.ingestion.alesc.sessoes import INDEX_PATH, iter_sessions
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.util import clean

logger = logging.getLogger("resumo.ingestion.alesc")

ORDEM_DO_DIA = "ordem-do-dia"


class VotacoesCollector(Collector):
    name = "alesc_votacoes"

    def run(
        self,
        session: Session,
        *,
        data_inicio: str | None = None,
        data_fim: str | None = None,
        id_legislatura: int | None = None,
        client: AlescClient | None = None,
        limit: int | None = None,
        max_pages: int = 100,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.alesc_id_legislatura
        index = mandate_index(session, leg)
        if not index:
            return CollectorResult(
                self.name, "empty", 0,
                f"no ASSEMBLEIA mandates for legislatura {leg} — run alesc-deputados first",
            )

        owns = client is None
        client = client or AlescClient()
        try:
            total = 0
            n_sessions = n_nominal = n_simbolica = 0
            for ref in iter_sessions(
                client,
                data_inicio=data_inicio,
                data_fim=data_fim,
                limit=limit,
                max_pages=max_pages,
                section=ORDEM_DO_DIA,
            ):
                n_sessions += 1
                markup = client.get_elegis(f"{INDEX_PATH}/{ref.session_hash}/{ORDEM_DO_DIA}")
                for item in parse_ordem_do_dia(markup):
                    if not item.is_nominal:
                        # Symbolic: no individual position exists. Produce NO Vote rows.
                        n_simbolica += 1
                        continue
                    n_nominal += 1
                    extrato = parse_extrato_votacao(
                        client.get_elegis(f"/extrato-votacao/{item.extrato_hash}", htmx=True)
                    )
                    rows = []
                    for nome, tipo_voto in extrato.votos:
                        member = index.match(nome)
                        if member is None:
                            continue
                        rows.append(
                            {
                                "mandate_id": member.mandate_id,
                                "house_member_id": member.slug,
                                "id_votacao": votacao_id(item.extrato_hash),
                                "id_proposicao": (
                                    proposition_id(item.proposicao_hash)
                                    if item.proposicao_hash
                                    else None
                                ),
                                "tipo_voto": (clean(tipo_voto) or "")[:32] or None,
                                "data_votacao": ref.data,
                                # ALESC publishes no party guidance for any votação.
                                "orientacao_partido": None,
                            }
                        )
                    total += upsert(
                        session, Vote, rows, index_elements=["id_votacao", "house_member_id"]
                    )

            unmatched = index.report_unmatched(self.name, "extrato de votação")
            share = (n_nominal / (n_nominal + n_simbolica) * 100) if (n_nominal + n_simbolica) else 0
            logger.info(
                "ALESC votações: %s nominal / %s items (%.1f%%) across %s sessions",
                n_nominal, n_nominal + n_simbolica, share, n_sessions,
            )
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=(
                    f"{settings.alesc_elegis_base}/sessoes-plenarias"
                    f"?{data_inicio or 'inicio'}..{data_fim or 'fim'}"
                ),
                digest=f"count={total}",
                row_count=total,
            )
            detail = (
                f"{n_sessions} sessions · {n_nominal} nominal / "
                f"{n_nominal + n_simbolica} items ({share:.1f}% nominal)"
            )
            if unmatched:
                detail += f" · {unmatched}"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()
