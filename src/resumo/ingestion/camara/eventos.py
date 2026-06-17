"""Collector: Câmara /eventos + /eventos/{id}/deputados -> AttendanceRecord.

Câmara has no first-class "presença" resource; we record presence from event
attendance lists. Absence ("faltas") is therefore DERIVED downstream (expected vs
present) and is always labeled as such to avoid disputes.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import AttendanceRecord
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.camara.common import mandate_map
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.util import parse_date


class EventosCollector(Collector):
    name = "camara_eventos"

    def run(
        self,
        session: Session,
        *,
        data_inicio: str,
        data_fim: str,
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
            eventos = list(
                client.paginate(
                    "eventos",
                    {"dataInicio": data_inicio, "dataFim": data_fim, "ordem": "ASC"},
                )
            )
            if limit:
                eventos = eventos[:limit]

            total = 0
            for ev in eventos:
                id_evento = str(ev["id"])
                data = parse_date((ev.get("dataHoraInicio") or "")[:10])
                tipo = (ev.get("descricaoTipo") or None)
                throttle()
                presentes = client.get(f"eventos/{id_evento}/deputados").get("dados", [])
                rows = []
                for dep in presentes:
                    member_id = str(dep.get("id") or "")
                    if not member_id:
                        continue
                    rows.append(
                        {
                            "mandate_id": mandates.get(member_id),
                            "house_member_id": member_id,
                            "id_evento": id_evento,
                            "data": data,
                            "tipo": tipo,
                            "presente": True,
                            "derivation": "camara_evento_presenca",
                        }
                    )
                total += upsert(
                    session, AttendanceRecord, rows, index_elements=["id_evento", "house_member_id"]
                )
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{get_settings().camara_api_base}/eventos?{data_inicio}..{data_fim}",
                digest=f"count={total}",
                row_count=total,
            )
            return CollectorResult(self.name, "ingested", total, f"{len(eventos)} eventos")
        finally:
            if owns:
                client.close()
