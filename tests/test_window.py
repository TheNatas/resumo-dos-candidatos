"""Janela incremental: refazer o ano inteiro toda execução custa ~14 mil
requisições e três horas para reaprender votações que não mudam."""

from __future__ import annotations

import datetime as dt

from resumo.db.models import House, Mandate, Person, Vote
from resumo.window import incremental_start

PISO = "2026-01-01"


def _voto(session, house: House, member: str, data: dt.date):
    person = Person(nome_normalizado=member, nome_civil=member)
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=house, house_member_id=member, id_legislatura=57,
        person_id=person.id, sigla_uf="SC", nome_parlamentar=member,
    )
    session.add(mandate)
    session.flush()
    session.add(
        Vote(mandate_id=mandate.id, house_member_id=member,
             id_votacao=f"V-{member}", tipo_voto="Sim", data_votacao=data)
    )
    session.commit()


def test_empty_database_asks_for_the_whole_year(session):
    assert incremental_start(session, House.CAMARA, floor=PISO) == PISO


def test_resumes_shortly_before_the_newest_vote(session):
    _voto(session, House.CAMARA, "1", dt.date(2026, 8, 12))

    assert incremental_start(session, House.CAMARA, floor=PISO, overlap_days=30) == "2026-07-13"


def test_overlap_exists_because_the_source_publishes_late(session):
    """Reler algumas semanas é barato; perder uma votação publicada com atraso
    não é recuperável sem alguém perceber."""
    _voto(session, House.CAMARA, "1", dt.date(2026, 8, 12))

    curta = incremental_start(session, House.CAMARA, floor=PISO, overlap_days=1)
    longa = incremental_start(session, House.CAMARA, floor=PISO, overlap_days=60)
    assert curta == "2026-08-11"
    assert longa < curta


def test_never_reaches_back_before_the_floor(session):
    """A janela encolhe, nunca se estende para trás do início do ciclo."""
    _voto(session, House.CAMARA, "1", dt.date(2026, 1, 5))

    assert incremental_start(session, House.CAMARA, floor=PISO, overlap_days=90) == PISO


def test_each_house_moves_at_its_own_pace(session):
    """Um máximo global faria a Casa mais lenta ser pulada pelo avanço da mais
    rápida — exatamente o caso real: Senado em agosto, Câmara ainda em 2024."""
    _voto(session, House.SENADO, "S1", dt.date(2026, 8, 12))
    _voto(session, House.CAMARA, "C1", dt.date(2024, 4, 24))

    assert incremental_start(session, House.SENADO, floor=PISO) == "2026-07-13"
    # A Câmara não pode ser arrastada para agosto por causa do Senado.
    assert incremental_start(session, House.CAMARA, floor=PISO) == PISO
    assert incremental_start(session, House.ASSEMBLEIA, floor=PISO) == PISO
