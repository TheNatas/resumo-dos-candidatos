"""Cross-house identity: `resolve_person` and the resolution rules that depend on it.

The contract under test is the one that makes a multi-house platform coherent — the
same human seen through Câmara (CPF), Senado (no CPF, has DOB) and ALESC (neither)
must land on ONE Person where that can be established, and on separate Persons where
it cannot. A wrong merge attributes one person's record to another, so the bar for
merging is deliberately higher than the bar for duplicating.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    ConfidenceTier,
    House,
    Mandate,
    Person,
)
from resumo.resolution.identity import resolve_person
from resumo.resolution.pipeline import resolve
from resumo.resolution.records import load_person_recs

DOB = dt.date(1947, 12, 21)


# ── resolve_person ───────────────────────────────────────────────────────────
def test_cpf_reunites_the_same_person(session):
    a = resolve_person(session, cpf="123.456.789-09", nome_civil="JOSE DA SILVA", dob=DOB)
    b = resolve_person(session, cpf="12345678909", nome_civil="JOSE DA SILVA FILHO")
    session.flush()
    assert a.id == b.id
    assert session.scalar(select(func.count()).select_from(Person)) == 1


def test_name_plus_dob_bridges_a_house_with_no_cpf(session):
    """The Senado publishes no CPF; this pairing is the only bridge back to the
    Câmara-seeded Person."""
    camara = resolve_person(
        session, cpf="12345678909", nome_civil="ESPERIDIAO AMIN HELOU FILHO", dob=DOB
    )
    senado = resolve_person(session, cpf=None, nome_civil="Esperidião Amin Helou Filho", dob=DOB)
    session.flush()

    assert senado.id == camara.id
    # The poorer source must not erase what the richer one established.
    assert senado.cpf == "12345678909"


def test_name_alone_never_merges(session):
    """ALESC gives a name and nothing else. Two people sharing a name must stay
    separate — a duplicate is recoverable, a wrong merge is not."""
    first = resolve_person(session, nome_civil="JOAO SOUZA")
    second = resolve_person(session, nome_civil="JOAO SOUZA")
    session.flush()

    assert first.id != second.id
    assert session.scalar(select(func.count()).select_from(Person)) == 2


def test_same_name_different_dob_stays_separate(session):
    a = resolve_person(session, nome_civil="MARIA LIMA", dob=dt.date(1960, 1, 1))
    b = resolve_person(session, nome_civil="MARIA LIMA", dob=dt.date(1985, 7, 9))
    session.flush()
    assert a.id != b.id


def test_conflicting_cpf_does_not_overwrite(session):
    """If two different CPFs reach one row, an upstream match was wrong. Keep the
    established value rather than silently corrupting it."""
    person = resolve_person(session, cpf="12345678909", nome_civil="ANA PEREIRA", dob=DOB)
    session.flush()
    again = resolve_person(session, cpf="98765432100", titulo=None, nome_civil="ANA PEREIRA", dob=DOB)
    session.flush()

    assert again.id == person.id  # matched on name+dob
    assert again.cpf == "12345678909"  # original kept


def test_titulo_matches_when_cpf_is_absent(session):
    a = resolve_person(session, titulo="123456789012", nome_civil="CARLOS DIAS")
    b = resolve_person(session, titulo="123456789012", nome_civil="C. DIAS")
    session.flush()
    assert a.id == b.id


# ── person loading across houses ─────────────────────────────────────────────
def _mandate(session, person, house, member, *, uf="SC", active=True, start=None):
    m = Mandate(
        house=house,
        house_member_id=member,
        id_legislatura=57,
        person_id=person.id,
        sigla_uf=uf,
        data_fim=None if active else dt.date(2023, 1, 31),
        data_inicio=start,
    )
    session.add(m)
    session.flush()
    return m


def test_load_person_recs_spans_every_house(session):
    """Regression: this used to filter to House.CAMARA, so senators and state
    deputies could never be resolved at all."""
    for i, house in enumerate((House.CAMARA, House.SENADO, House.ASSEMBLEIA)):
        p = resolve_person(session, nome_civil=f"PESSOA {i}", dob=dt.date(1970, 1, i + 1))
        _mandate(session, p, house, str(i))
    session.commit()

    recs = load_person_recs(session)
    assert {r.house for r in recs} == {House.CAMARA, House.SENADO, House.ASSEMBLEIA}


def test_active_mandate_wins_over_ended_one(session):
    p = resolve_person(session, cpf="12345678909", nome_civil="X Y", dob=DOB)
    _mandate(session, p, House.CAMARA, "old", active=False, start=dt.date(2019, 2, 1))
    active = _mandate(session, p, House.SENADO, "new", active=True, start=dt.date(2023, 2, 1))
    session.commit()

    recs = [r for r in load_person_recs(session) if r.person_id == p.id]
    assert len(recs) == 1
    assert recs[0].mandate_id == active.id


# ── tiering: a name-only match must not reach auto_strong ────────────────────
def _candidacy(session, sq, **kw):
    defaults = dict(ano_eleicao=2026, nr_turno=1, sg_uf="SC", cd_cargo=7)
    defaults.update(kw)
    session.add(Candidacy(sq_candidato=sq, **defaults))


def test_name_only_match_is_capped_at_auto_weak(session):
    """An ALESC-style mandate has no CPF and no birth date. A perfect name match is
    still only a name match, and must be published as auto_weak — never as the same
    tier as a CPF match."""
    p = resolve_person(session, nome_civil="JOAO DA COSTA")
    _mandate(session, p, House.ASSEMBLEIA, "joao-da-costa")
    _candidacy(session, "E1", nome_normalizado="JOAO DA COSTA", cpf_raw=None, data_nascimento=None)
    session.commit()

    resolve(session, year=2026)
    session.commit()

    link = session.scalar(select(CandidateMandateLink))
    assert link is not None, "a name-only match should still publish, just at a lower tier"
    assert link.confidence_tier is ConfidenceTier.auto_weak


def test_matching_dob_still_reaches_auto_strong(session):
    """Contrast with the above: once a birth date corroborates the name, the match
    is allowed into the top tier."""
    p = resolve_person(session, nome_civil="JOAO DA COSTA", dob=DOB)
    _mandate(session, p, House.SENADO, "22")
    _candidacy(
        session, "S1", cd_cargo=5, nome_normalizado="JOAO DA COSTA", cpf_raw=None,
        data_nascimento=DOB,
    )
    session.commit()

    resolve(session, year=2026)
    session.commit()

    link = session.scalar(select(CandidateMandateLink))
    assert link is not None
    assert link.confidence_tier is ConfidenceTier.auto_strong


def test_candidacy_links_across_houses(session):
    """A sitting deputado federal running for senador must still get their Câmara
    history — the link is 'holds a mandate', not 'seeks the same seat'."""
    p = resolve_person(session, cpf="12345678909", nome_civil="DEPUTADA X", dob=DOB)
    camara = _mandate(session, p, House.CAMARA, "1")
    _candidacy(session, "S9", cd_cargo=5, cpf_raw="123.456.789-09", nome_normalizado="DEPUTADA X")
    session.commit()

    resolve(session, year=2026)
    session.commit()

    link = session.scalar(select(CandidateMandateLink))
    assert link is not None
    assert link.mandate_id == camara.id
    assert link.confidence_tier is ConfidenceTier.auto_strong
