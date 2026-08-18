"""Lightweight records + loaders for the resolution pipeline.

Person side comes from every house we collect mandates for — Câmara, Senado and the
state assembly. A candidacy is matched against ALL of them, deliberately: a deputado
federal running for senador is the common case, and their Câmara record is exactly
the history a reader wants. The link therefore means "this candidate currently holds
a mandate", not "is running for the same seat".

Identity strength varies sharply by house, and the pipeline has to know:
  * Câmara  — CPF published -> deterministic match.
  * Senado  — NO CPF anywhere in its API; nome civil + data de nascimento only.
  * ALESC   — NO CPF and NO birth date; name only.
`PersonRec.has_corroborator` carries that fact forward so a name-only match cannot
be promoted to the strongest tier. See resolution/probabilistic.py.
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
    titulo: str | None
    nome_norm: str | None
    dob: dt.date | None
    uf: str | None
    house: House
    mandate_active: bool

    @property
    def has_corroborator(self) -> bool:
        """Whether anything beyond the name can confirm this identity."""
        return bool(self.cpf or self.titulo or self.dob)


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


def load_person_recs(session: Session, *, houses: list[House] | None = None) -> list[PersonRec]:
    """One PersonRec per (person, chosen mandate), across every collected house.

    When a person holds several mandates, an active one wins; ties break on the
    later `data_inicio` so the most recent seat represents them."""
    stmt = select(Mandate, Person).join(Person, Mandate.person_id == Person.id)
    if houses:
        stmt = stmt.where(Mandate.house.in_(houses))
    rows = session.execute(stmt).all()

    best: dict[uuid.UUID, PersonRec] = {}
    starts: dict[uuid.UUID, dt.date | None] = {}
    for mandate, person in rows:
        active = mandate.data_fim is None
        rec = PersonRec(
            person_id=person.id,
            mandate_id=mandate.id,
            member_id=mandate.house_member_id,
            cpf=person.cpf,
            titulo=person.titulo_eleitoral,
            nome_norm=person.nome_normalizado,
            dob=person.data_nascimento,
            uf=mandate.sigla_uf,
            house=mandate.house,
            mandate_active=active,
        )
        prev = best.get(person.id)
        if prev is None:
            best[person.id], starts[person.id] = rec, mandate.data_inicio
            continue
        prev_start = starts.get(person.id)
        newer = (mandate.data_inicio or dt.date.min) > (prev_start or dt.date.min)
        if (active and not prev.mandate_active) or (active == prev.mandate_active and newer):
            best[person.id], starts[person.id] = rec, mandate.data_inicio
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
