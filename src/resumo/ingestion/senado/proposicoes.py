"""Collector: Senado /processo?codigoParlamentarAutor= -> Proposition (bills signed).

The `/materia/*` tree is deprecated and must not be used; `/processo` is the modern
replacement (bare JSON array, camelCase, native types).

⚠️ AUTHORSHIP IS NOT PRIMARY AUTHORSHIP HERE. `/processo` exposes `autoria` as one
free-text string listing every co-author ("Senador X (PP/SC), Senador Y (PT/BA)")
and carries **no** first-author flag. Querying by `codigoParlamentarAutor` therefore
answers "this senator signed it", never "this senator wrote it" — the public surface
must not claim otherwise. A consequence for the shared `Proposition` row: a bill
co-signed by two senators we track resolves to a single row whose
`authoring_mandate_id` is whichever of them was collected last.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import House, Proposition
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.ingestion.senado.client import SenadoClient
from resumo.ingestion.senado.common import mandate_map
from resumo.util import clean, parse_date, parse_int


def _prop_row(mandate_id, p: dict) -> dict | None:
    processo_id = clean(p.get("id"))
    if not processo_id:
        return None
    return {
        # Proposition.proposition_id is shared with the Câmara, whose ids are bare
        # integers — an unprefixed Senado id would eventually collide on the PK.
        "proposition_id": f"SF{processo_id}",
        "house": House.SENADO,
        "authoring_mandate_id": mandate_id,
        "sigla_tipo": clean(p.get("sigla")),
        "numero": parse_int(p.get("numero")),
        "ano": parse_int(p.get("ano")),
        "ementa": clean(p.get("ementa")),
        "data_apresentacao": parse_date(p.get("dataApresentacao")),
        "situacao": clean(p.get("situacaoAtual")),
    }


class ProposicoesCollector(Collector):
    name = "senado_proposicoes"

    def run(
        self,
        session: Session,
        *,
        id_legislatura: int | None = None,
        client: SenadoClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.id_legislatura
        owns = client is None
        client = client or SenadoClient()
        try:
            mandates = mandate_map(session, leg)
            members = list(mandates.items())
            if limit:
                members = members[:limit]

            total = 0
            for member_id, mandate_id in members:
                throttle()
                payload = client.get(
                    "processo",
                    # `tramitouLegislaturaAtual=S` scopes to the running legislatura;
                    # without it the endpoint returns a senator's entire career.
                    {"codigoParlamentarAutor": member_id, "tramitouLegislaturaAtual": "S"},
                )
                rows = [
                    row
                    for p in (payload if isinstance(payload, list) else [])
                    if (row := _prop_row(mandate_id, p))
                ]
                total += upsert(session, Proposition, rows, index_elements=["proposition_id"])
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{settings.senado_api_base}/processo?codigoParlamentarAutor=*",
                digest=f"count={total}",
                row_count=total,
            )
            return CollectorResult(self.name, "ingested", total, f"{len(members)} senadores")
        finally:
            if owns:
                client.close()
