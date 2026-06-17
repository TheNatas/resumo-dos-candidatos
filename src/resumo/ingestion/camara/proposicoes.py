"""Collector: Câmara /proposicoes?idDeputadoAutor= -> Proposition (authored bills)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import House, Proposition
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.camara.common import mandate_map
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.util import clean, parse_date, parse_int


def _prop_row(mandate_id, p: dict) -> dict:
    return {
        "proposition_id": str(p["id"]),
        "house": House.CAMARA,
        "authoring_mandate_id": mandate_id,
        "sigla_tipo": clean(p.get("siglaTipo")),
        "numero": parse_int(p.get("numero")),
        "ano": parse_int(p.get("ano")),
        "ementa": clean(p.get("ementa")),
        "data_apresentacao": parse_date(p.get("dataApresentacao")),
    }


class ProposicoesCollector(Collector):
    name = "camara_proposicoes"

    def run(
        self,
        session: Session,
        *,
        id_legislatura: int | None = None,
        client: CamaraClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        leg = id_legislatura or get_settings().id_legislatura
        owns = client is None
        client = client or CamaraClient()
        try:
            mandates = mandate_map(session, leg)
            members = list(mandates.items())
            if limit:
                members = members[:limit]

            total = 0
            for member_id, mandate_id in members:
                throttle()
                rows = [
                    _prop_row(mandate_id, p)
                    for p in client.paginate(
                        # NB: /proposicoes rejects idLegislatura (code 5); idDeputadoAutor scopes it.
                        "proposicoes",
                        {"idDeputadoAutor": member_id, "ordem": "DESC", "ordenarPor": "id"},
                    )
                ]
                total += upsert(session, Proposition, rows, index_elements=["proposition_id"])
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{get_settings().camara_api_base}/proposicoes?idDeputadoAutor=*",
                digest=f"count={total}",
                row_count=total,
            )
            return CollectorResult(self.name, "ingested", total, f"{len(members)} deputies")
        finally:
            if owns:
                client.close()
