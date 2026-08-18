"""Collector: Câmara /deputados (+ detail) -> Mandate, and seeds Person identity.

The list endpoint lacks CPF; only /deputados/{id} exposes cpf/nomeCivil/dataNascimento
— the anchor for deterministic entity resolution. So we list, then fan out to detail.
Person rows are seeded here (Câmara is authoritative for a deputy's CPF).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import House, Mandate, Person
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import record_ingestion
from resumo.resolution.identity import resolve_person
from resumo.util import clean, parse_date

logger = logging.getLogger("resumo.ingestion.camara")


def _get_or_create_person(session: Session, detail: dict) -> Person | None:
    """Câmara is the richest identity source (it is the only house that publishes
    CPF), but the Person it resolves to may already exist from another house — so
    this goes through the shared resolver rather than matching on CPF alone."""
    return resolve_person(
        session,
        cpf=detail.get("cpf"),
        nome_civil=detail.get("nomeCivil"),
        dob=parse_date(detail.get("dataNascimento")),
        uf_nascimento=detail.get("ufNascimento"),
    )


def _upsert_mandate(session: Session, member_id: str, leg: int, status: dict, person: Person | None) -> None:
    existing = session.execute(
        select(Mandate).where(
            Mandate.house == House.CAMARA,
            Mandate.house_member_id == member_id,
            Mandate.id_legislatura == leg,
        )
    ).scalar_one_or_none()
    mandate = existing or Mandate(
        house=House.CAMARA, house_member_id=member_id, id_legislatura=leg
    )
    mandate.person_id = person.id if person else mandate.person_id
    mandate.nome_parlamentar = clean(status.get("nomeEleitoral")) or mandate.nome_parlamentar
    mandate.sigla_partido = clean(status.get("siglaPartido"))
    mandate.sigla_uf = clean(status.get("siglaUf"))
    mandate.condicao_eleitoral = clean(status.get("condicaoEleitoral"))
    mandate.situacao = clean(status.get("situacao"))
    mandate.data_inicio = mandate.data_inicio or parse_date(status.get("data"))
    if existing is None:
        session.add(mandate)
    session.flush()


class DeputadosCollector(Collector):
    name = "camara_deputados"

    def run(
        self,
        session: Session,
        *,
        id_legislatura: int | None = None,
        client: CamaraClient | None = None,
        limit: int | None = None,
        ufs: list[str] | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.id_legislatura
        uf_scope = tuple(u.upper() for u in ufs) if ufs is not None else settings.uf_list
        owns = client is None
        client = client or CamaraClient()
        try:
            params = {"idLegislatura": leg, "ordem": "ASC", "ordenarPor": "nome"}
            if uf_scope:
                # The API takes siglaUf repeated; httpx encodes a list as repeats.
                params["siglaUf"] = list(uf_scope)
            listed = list(client.paginate("deputados", params))
            if limit:
                listed = listed[:limit]

            count = 0
            for item in listed:
                member_id = str(item["id"])
                throttle()
                detail = client.get(f"deputados/{member_id}")["dados"]
                status = detail.get("ultimoStatus") or {}
                leg_of = status.get("idLegislatura") or leg
                person = _get_or_create_person(session, detail)
                _upsert_mandate(session, member_id, int(leg_of), status, person)
                count += 1

            record_ingestion(
                session,
                collector_name=self.name,
                source_url=(
                    f"{settings.camara_api_base}/deputados?idLegislatura={leg}"
                    + (f"&siglaUf={','.join(uf_scope)}" if uf_scope else "")
                ),
                digest=f"count={count}",
                row_count=count,
            )
            scope = f"legislatura {leg} · uf={','.join(uf_scope) or 'ALL'}"
            return CollectorResult(self.name, "ingested", count, scope)
        finally:
            if owns:
                client.close()
