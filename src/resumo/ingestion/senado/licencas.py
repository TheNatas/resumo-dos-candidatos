"""Collector: Senado ``/senador/{codigo}/licencas`` -> MandateLeave.

A única ausência que o Senado publica **em dias** — e com o motivo declarado pela
própria Casa (missão política, licença para tratamento de saúde, licença de atividade
parlamentar). Datas de início e fim, portanto dias corridos de calendário.

🚨 **Isto não entra na conta de presença.** A régua é outra: presença no Senado só é
observável nas sessões com votação nominal, e uma licença de 30 dias não equivale a
30 sessões. Somar os dois produziria um número que nenhuma fonte publica. A licença
fica ao lado, respondendo *por que* o senador não estava lá.

Armadilhas do serviço (legado XML→JSON, verificadas ao vivo 2026-08-19):

* o envelope é ``LicencaParlamentar/Parlamentar/Licencas/Licenca`` e some inteiro
  quando o senador não tem licença — a ausência da chave é o caso normal, não um erro;
* um único elemento vem como objeto, vários como lista (:func:`_as_list`);
* código inexistente devolve **200** sem o nó ``Parlamentar``, nunca 404;
* sem o header ``Accept: application/json`` a resposta vem em XML (o
  :class:`SenadoClient` já o fixa; ``?formato=json`` é ignorado pelo serviço).
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from resumo.attendance import leave_days
from resumo.config import get_settings
from resumo.db.models import House, MandateLeave
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import content_hash, record_ingestion, upsert
from resumo.ingestion.senado.client import SenadoClient, _as_list, dig
from resumo.ingestion.senado.common import mandate_map
from resumo.util import clean, parse_date

logger = logging.getLogger("resumo.ingestion.senado")


class LicencasCollector(Collector):
    name = "senado_licencas"

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
        mandates = mandate_map(session, leg)
        if not mandates:
            return CollectorResult(
                self.name, "empty", 0,
                f"no SENADO mandates for legislatura {leg} — run senado-senadores first",
            )
        members = sorted(mandates)[:limit] if limit else sorted(mandates)

        owns = client is None
        client = client or SenadoClient()
        try:
            total = 0
            falhas = 0
            sem_licenca = 0
            dias = 0
            for member_id in members:
                path = f"senador/{member_id}/licencas"
                try:
                    payload = client.get(path)
                except httpx.HTTPError as exc:
                    falhas += 1
                    logger.warning("%s: %s falhou (%s) — pulado", self.name, path, exc)
                    continue

                licencas = _as_list(
                    dig(payload, "LicencaParlamentar", "Parlamentar", "Licencas", "Licenca")
                )
                if not licencas:
                    sem_licenca += 1
                    continue

                rows = []
                for lic in licencas:
                    leave_id = clean(lic.get("Codigo"))
                    if not leave_id:
                        # Sem id não há chave natural; um upsert por (mandato, datas)
                        # duplicaria a cada recoleta se a fonte reeditar as datas.
                        continue
                    inicio = parse_date(lic.get("DataInicio"))
                    fim = parse_date(lic.get("DataFim"))
                    dias += leave_days(inicio, fim) or 0
                    rows.append(
                        {
                            "mandate_id": mandates[member_id],
                            "house": House.SENADO,
                            "house_member_id": member_id,
                            "leave_id": leave_id,
                            "data_inicio": inicio,
                            "data_fim": fim,
                            "sigla_tipo": (clean(lic.get("SiglaTipoAfastamento")) or None),
                            "descricao_tipo": (
                                clean(lic.get("DescricaoTipoAfastamento")) or None
                            ),
                        }
                    )
                total += upsert(
                    session, MandateLeave, rows, index_elements=["mandate_id", "leave_id"]
                )

            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{settings.senado_api_base.rstrip('/')}/senador/*/licencas",
                digest=content_hash(f"{len(members)}:{total}:{dias}"),
                row_count=total,
            )
            detail = f"{len(members)} senadores · {dias} dia(s) de licença"
            if sem_licenca:
                detail += f" · {sem_licenca} sem licença"
            if falhas:
                detail += f" · {falhas} falha(s)"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()
