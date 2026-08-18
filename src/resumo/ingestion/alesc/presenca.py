"""Collector: ALESC presença por sessão -> AttendanceRecord.

Source of truth: ``{alesc_elegis_base}/sessoes-plenarias/{hash}/presenca`` — a plain
``<table class="table table-hover">``, one row per deputy, values ``Presente`` /
``Ausência justificada``.

Unlike Câmara (where absence has to be *derived* by cross-referencing event
attendance), ALESC states attendance directly, so `presente` is observed rather than
inferred. `derivation` still records where the row came from
(``alesc_sessao_presenca``) so the two houses stay comparable and auditable.

Only sessions whose ``presenca`` link is enabled are fetched: the index marks the link
``disabled`` when the roll was not published (typically extraordinary sessions).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import AttendanceRecord
from resumo.ingestion.alesc.client import AlescClient
from resumo.ingestion.alesc.common import evento_id, mandate_index
from resumo.ingestion.alesc.parsing import parse_presenca
from resumo.ingestion.alesc.sessoes import INDEX_PATH, iter_sessions
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import record_ingestion, upsert

logger = logging.getLogger("resumo.ingestion.alesc")

PRESENCA = "presenca"
DERIVATION = "alesc_sessao_presenca"


class PresencaCollector(Collector):
    name = "alesc_presenca"

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
            n_sessions = 0
            for ref in iter_sessions(
                client,
                data_inicio=data_inicio,
                data_fim=data_fim,
                limit=limit,
                max_pages=max_pages,
                section=PRESENCA,
            ):
                n_sessions += 1
                markup = client.get_elegis(f"{INDEX_PATH}/{ref.session_hash}/{PRESENCA}")
                rows = []
                for entry in parse_presenca(markup):
                    member = index.match(entry.nome)
                    if member is None:
                        continue
                    rows.append(
                        {
                            "mandate_id": member.mandate_id,
                            "house_member_id": member.slug,
                            "id_evento": evento_id(ref.session_hash),
                            "data": ref.data,
                            "tipo": (ref.titulo or "Sessão Plenária")[:64],
                            "presente": entry.presente,
                            "justificativa": (entry.justificativa or None),
                            "derivation": DERIVATION,
                        }
                    )
                total += upsert(
                    session, AttendanceRecord, rows,
                    index_elements=["id_evento", "house_member_id"],
                )

            unmatched = index.report_unmatched(self.name, "tabela de presença")
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=(
                    f"{settings.alesc_elegis_base}/sessoes-plenarias/*/presenca"
                    f"?{data_inicio or 'inicio'}..{data_fim or 'fim'}"
                ),
                digest=f"count={total}",
                row_count=total,
            )
            detail = f"{n_sessions} sessions"
            if unmatched:
                detail += f" · {unmatched}"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()
