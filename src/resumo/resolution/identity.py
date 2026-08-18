"""Canonical `Person` resolution shared by every mandate collector.

Each house publishes a different amount of identity, so seeding a Person from one of
them in isolation fragments the same human into several rows — a senator who served
two terms as a deputy would otherwise get a Câmara Person (with CPF) and a separate
Senado Person (without), and their Câmara track record would never attach to their
Senate candidacy.

What each source actually gives us:

===========  =====  ======  =============  ===========================================
house        CPF    título  nascimento     bridge available
===========  =====  ======  =============  ===========================================
Câmara       yes    no      yes            CPF (deterministic)
Senado       NO     no      yes            nome civil + nascimento
ALESC        NO     no      NO             name only -> deliberately NOT enough
TSE          maybe  yes     yes            CPF when unmasked, else título
===========  =====  ======  =============  ===========================================

**Name alone never merges two records.** Two people who share a name are
indistinguishable without a corroborating field, and a wrong merge is far worse than
a duplicate: it would attribute one person's votes and expenses to another. So a
name-only source (ALESC) always creates its own Person, and the candidate<->mandate
link is left to the resolution pipeline, which records confidence and is reviewable.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.db.models import Person
from resumo.util import clean, normalize_name, valid_cpf

logger = logging.getLogger("resumo.resolution.identity")


def resolve_person(
    session: Session,
    *,
    cpf: str | None = None,
    titulo: str | None = None,
    nome_civil: str | None = None,
    dob: dt.date | None = None,
    uf_nascimento: str | None = None,
) -> Person:
    """Find-or-create the canonical Person for a mandate holder.

    Matching order, strongest first: CPF, título eleitoral, then
    (nome_normalizado + data_nascimento). Returns an existing row when one of those
    hits, else a new one. Identity fields are enriched but never blanked — a source
    that lacks CPF must not erase a CPF another source already established.
    """
    cpf = valid_cpf(cpf)
    titulo = clean(titulo)
    nome_civil = clean(nome_civil)
    nome_norm = normalize_name(nome_civil)

    person: Person | None = None

    if cpf:
        person = session.execute(select(Person).where(Person.cpf == cpf)).scalar_one_or_none()

    if person is None and titulo:
        person = session.execute(
            select(Person).where(Person.titulo_eleitoral == titulo)
        ).scalar_one_or_none()

    # The only bridge to a house that publishes no CPF. Requires BOTH parts: a name
    # on its own is not an identifier (see the module docstring).
    if person is None and nome_norm and dob:
        person = session.execute(
            select(Person).where(
                Person.nome_normalizado == nome_norm, Person.data_nascimento == dob
            )
        ).scalar_one_or_none()

    if person is None:
        person = Person()
        session.add(person)
    elif cpf and person.cpf and person.cpf != cpf:
        # Two different CPFs reaching the same row means an upstream match was wrong.
        # Refuse to silently overwrite; keep the established value and shout.
        logger.warning(
            "identity conflict for %s: existing cpf differs from incoming; keeping existing",
            nome_norm or person.id,
        )
        cpf = None

    # Enrich only. `or existing` everywhere so a poorer source never downgrades a
    # richer one.
    person.cpf = cpf or person.cpf
    person.titulo_eleitoral = titulo or person.titulo_eleitoral
    person.nome_civil = nome_civil or person.nome_civil
    person.nome_normalizado = nome_norm or person.nome_normalizado
    person.data_nascimento = dob or person.data_nascimento
    person.uf_nascimento = clean(uf_nascimento) or person.uf_nascimento
    session.flush()
    return person
