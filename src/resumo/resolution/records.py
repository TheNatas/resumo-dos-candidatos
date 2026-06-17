"""Lightweight records + loaders for the resolution pipeline.

Person side comes from Câmara-seeded persons that hold a mandate (the only ones a
candidacy can be an "incumbent re-election" of). Candidate side comes from TSE.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.db.models import Candidacy, House, Mandate, Person
from resumo.util import valid_cpf


@dataclass
class PersonRec:
    person_id: uuid.UUID
    mandate_id: uuid.UUID
    member_id: str
    cpf: str | None
    nome_norm: str | None
    dob: dt.date | None
    uf: str | None
    mandate_active: bool


@dataclass
class CandRec:
    sq_candidato: str
    cpf: str | None
    titulo: str | None
    nome_norm: str | None
    dob: dt.date | None
    uf: str | None
    cd_cargo: int | None
    extra: dict = field(default_factory=dict)


def load_person_recs(session: Session) -> list[PersonRec]:
    """One PersonRec per (person, chosen mandate). Picks the active/most-recent
    Câmara mandate when a person has several."""
    rows = session.execute(
        select(Mandate, Person)
        .join(Person, Mandate.person_id == Person.id)
        .where(Mandate.house == House.CAMARA)
    ).all()

    best: dict[uuid.UUID, PersonRec] = {}
    for mandate, person in rows:
        active = mandate.data_fim is None
        rec = PersonRec(
            person_id=person.id,
            mandate_id=mandate.id,
            member_id=mandate.house_member_id,
            cpf=person.cpf,
            nome_norm=person.nome_normalizado,
            dob=person.data_nascimento,
            uf=mandate.sigla_uf,
            mandate_active=active,
        )
        prev = best.get(person.id)
        if prev is None or (active and not prev.mandate_active):
            best[person.id] = rec
    return list(best.values())


def load_candidacy_recs(session: Session, *, year: int | None = None) -> list[CandRec]:
    stmt = select(Candidacy)
    if year is not None:
        stmt = stmt.where(Candidacy.ano_eleicao == year)
    recs = []
    for c in session.execute(stmt).scalars():
        recs.append(
            CandRec(
                sq_candidato=c.sq_candidato,
                cpf=valid_cpf(c.cpf_raw),
                titulo=c.titulo_raw,
                nome_norm=c.nome_normalizado,
                dob=c.data_nascimento,
                uf=c.sg_uf,
                cd_cargo=c.cd_cargo,
                extra={"nome": c.nome_candidato, "partido": c.sg_partido, "cargo": c.ds_cargo},
            )
        )
    return recs
