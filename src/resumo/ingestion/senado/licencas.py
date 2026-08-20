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
  :class:`SenadoClient` já o fixa; ``?formato=json`` é ignorado pelo serviço);
* 🚨 o serviço devolve a **carreira inteira** do senador, não o mandato pedido —
  Acir Gurgacz volta a 2009, três mandatos atrás. Só entram as licenças que
  intersectam a janela do mandato (:func:`_in_window`), senão a ficha somaria à
  legislatura atual licenças de dez anos antes.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.attendance import leave_days
from resumo.config import get_settings
from resumo.db.models import House, Mandate, MandateLeave
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import content_hash, record_ingestion, upsert
from resumo.ingestion.senado.client import SenadoClient, _as_list, dig
from resumo.ingestion.senado.common import mandate_map
from resumo.util import clean, parse_date

logger = logging.getLogger("resumo.ingestion.senado")

Janela = tuple[dt.date | None, dt.date | None]


def _mandate_windows(session: Session, mandate_ids: list[uuid.UUID]) -> dict[uuid.UUID, Janela]:
    return {
        mid: (inicio, fim)
        for mid, inicio, fim in session.execute(
            select(Mandate.id, Mandate.data_inicio, Mandate.data_fim).where(
                Mandate.id.in_(mandate_ids)
            )
        )
    }


def _in_window(inicio: dt.date | None, fim: dt.date | None, janela: Janela | None) -> bool:
    """A licença pertence ao mandato em curso?

    🚨 ``/licencas`` devolve a **carreira inteira** do senador, não o mandato: Acir
    Gurgacz volta a 2009, três mandatos atrás. Somar tudo num bloco que fala do
    mandato atual seria atribuir a ele licenças de dez anos antes, então só entram as
    que intersectam a janela do mandato.

    Sem `data_inicio` não há como filtrar — aí tudo passa (e o coletor avisa), porque
    descartar em silêncio seria pior do que exibir demais.
    """
    if janela is None or janela[0] is None:
        return True
    mandato_inicio, mandato_fim = janela
    if fim is not None and fim < mandato_inicio:
        return False
    if mandato_fim is not None and inicio is not None and inicio > mandato_fim:
        return False
    return True


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
        janelas = _mandate_windows(session, list(mandates.values()))

        owns = client is None
        client = client or SenadoClient()
        try:
            total = 0
            falhas = 0
            sem_licenca = 0
            dias = 0
            fora_da_janela = 0
            sem_janela = set()
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

                mandate_id = mandates[member_id]
                janela = janelas.get(mandate_id)
                if janela is None or janela[0] is None:
                    sem_janela.add(member_id)

                rows = []
                for lic in licencas:
                    leave_id = clean(lic.get("Codigo"))
                    if not leave_id:
                        # Sem id não há chave natural; um upsert por (mandato, datas)
                        # duplicaria a cada recoleta se a fonte reeditar as datas.
                        continue
                    inicio = parse_date(lic.get("DataInicio"))
                    fim = parse_date(lic.get("DataFim"))
                    if not _in_window(inicio, fim, janela):
                        fora_da_janela += 1
                        continue
                    dias += leave_days(inicio, fim) or 0
                    rows.append(
                        {
                            "mandate_id": mandate_id,
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
            if fora_da_janela:
                detail += f" · {fora_da_janela} de mandatos anteriores, descartada(s)"
            if sem_janela:
                # Sem janela nada é filtrado, e a ficha passaria a somar a carreira
                # inteira num bloco que fala do mandato atual. Dizer isso alto é o
                # mínimo; corrigir é rodar `senado-senadores` antes.
                logger.warning(
                    "%s: %s senador(es) sem janela de mandato — licenças de mandatos "
                    "anteriores não puderam ser separadas. Rode `senado-senadores` antes.",
                    self.name, len(sem_janela),
                )
                detail += f" · {len(sem_janela)} sem janela de mandato"
            if falhas:
                detail += f" · {falhas} falha(s)"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()
