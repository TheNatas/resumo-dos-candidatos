from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    House,
    Mandate,
    Person,
    ReviewQueue,
)
from resumo.resolution.pipeline import resolve


def _seed_person_mandate(session, cpf, name_norm, *, uf="SP", member="1", dob="1970-05-10"):
    person = Person(
        cpf=cpf, nome_normalizado=name_norm, data_nascimento=dt.date.fromisoformat(dob), uf_nascimento=uf
    )
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=House.CAMARA, house_member_id=member, id_legislatura=57,
        person_id=person.id, sigla_uf=uf, data_fim=None,
    )
    session.add(mandate)
    session.flush()
    return person, mandate


def _add_candidacy(session, sq, **kw):
    defaults = dict(ano_eleicao=2022, nr_turno=1, sg_uf="SP", cd_cargo=6)
    defaults.update(kw)
    session.add(Candidacy(sq_candidato=sq, **defaults))


def test_cpf_exact_creates_strong_link(session):
    _seed_person_mandate(session, "12345678909", "JOSE DA SILVA")
    _add_candidacy(session, "C1", cpf_raw="123.456.789-09", nome_normalizado="JOSE DA SILVA")
    session.commit()

    result = resolve(session, year=2022)
    session.commit()

    link = session.scalar(select(CandidateMandateLink))
    assert link is not None
    assert link.match_method.value == "cpf_exact"
    assert link.confidence_tier.value == "auto_strong"
    assert link.is_incumbent_reelection is True
    assert result.links == 1
    # Candidacy.person_id back-filled
    assert session.get(Candidacy, "C1").person_id is not None


def test_homonym_routes_to_review_not_link(session):
    _seed_person_mandate(session, "11111111191", "JOAO SOUZA", member="1")
    _seed_person_mandate(session, "22222222272", "JOAO SOUZA", member="2")
    # No CPF -> probabilistic; identical name to both -> ambiguous.
    _add_candidacy(session, "C9", cpf_raw=None, nome_normalizado="JOAO SOUZA", data_nascimento=None)
    session.commit()

    resolve(session, year=2022)
    session.commit()

    assert session.scalar(select(func.count()).select_from(CandidateMandateLink)) == 0
    assert session.scalar(select(func.count()).select_from(ReviewQueue)) >= 1


def test_unmatched_candidacy_gets_no_link(session):
    _seed_person_mandate(session, "12345678909", "JOSE DA SILVA", uf="SP")
    _add_candidacy(session, "CX", cpf_raw=None, nome_normalizado="ANA PEREIRA", sg_uf="RJ")
    session.commit()

    result = resolve(session, year=2022)
    session.commit()

    assert session.scalar(select(func.count()).select_from(CandidateMandateLink)) == 0
    assert result.unmatched == 1


def test_resolution_is_idempotent(session):
    _seed_person_mandate(session, "12345678909", "JOSE DA SILVA")
    _add_candidacy(session, "C1", cpf_raw="123.456.789-09", nome_normalizado="JOSE DA SILVA")
    session.commit()

    resolve(session, year=2022)
    session.commit()
    resolve(session, year=2022)
    session.commit()

    assert session.scalar(select(func.count()).select_from(CandidateMandateLink)) == 1
