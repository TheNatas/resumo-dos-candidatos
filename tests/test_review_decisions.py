"""Versioned review decisions: parsing, contradictions, and the property the whole
design exists for — surviving a rebuild that regenerates every uuid."""

from __future__ import annotations

import pytest

from resumo.db.models import (
    Candidacy,
    House,
    Mandate,
    Person,
    ReviewQueue,
    ReviewStatus,
)
from resumo.review import (
    DecisionFileError,
    apply_decisions,
    export_pending,
    parse_decisions,
)

FILE = """
version: 1
decisions:
  - sq_candidato: "C1"
    house: CAMARA
    house_member_id: "204521"
    id_legislatura: 57
    decision: match
    by: natanael
    note: nome civil e nascimento conferem
"""


def _seed(session, *, status=ReviewStatus.pending):
    person = Person(cpf=None, nome_normalizado="JOSE DA SILVA", nome_civil="JOSE DA SILVA")
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=House.CAMARA, house_member_id="204521", id_legislatura=57,
        person_id=person.id, sigla_uf="SC", nome_parlamentar="JOSE",
    )
    session.add(mandate)
    session.flush()
    session.add(
        Candidacy(
            sq_candidato="C1", ano_eleicao=2026, sg_uf="SC", cd_cargo=6,
            ds_cargo="DEPUTADO FEDERAL", nome_candidato="JOSE DA SILVA",
            nome_normalizado="JOSE DA SILVA", sg_partido="PT",
        )
    )
    session.add(
        ReviewQueue(
            sq_candidato="C1", mandate_id=mandate.id, suggested_score=0.88,
            reason="nome apenas", status=status,
        )
    )
    session.commit()
    return mandate


def test_parses_a_valid_file():
    (decision,) = parse_decisions(FILE)
    assert decision.sq_candidato == "C1"
    assert decision.house is House.CAMARA
    assert decision.decision is ReviewStatus.match
    assert decision.by == "natanael"


def test_accepts_either_case_for_hand_edited_values():
    (decision,) = parse_decisions(FILE.replace("CAMARA", "camara").replace("match", "MATCH"))
    assert decision.house is House.CAMARA
    assert decision.decision is ReviewStatus.match


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("version: 1", "version"),                      # version ausente vira None
        ('decision: match', "decision"),                # decisão inválida abaixo
        ('house: CAMARA', "house"),
        ('id_legislatura: 57', "id_legislatura"),
    ],
)
def test_rejects_malformed_entries(mutation, message):
    broken = FILE.replace(mutation, mutation.split(":")[0] + ": ~")
    with pytest.raises(DecisionFileError, match=message):
        parse_decisions(broken)


def test_rejects_pending_as_a_decision():
    """`pending` is the absence of a judgement; writing it down would be a no-op
    dressed up as a decision."""
    with pytest.raises(DecisionFileError, match="decision deve ser uma de"):
        parse_decisions(FILE.replace("decision: match", "decision: pending"))


def test_rejects_two_decisions_for_the_same_pair():
    with pytest.raises(DecisionFileError, match="mesmo par"):
        parse_decisions(FILE + FILE.split("decisions:")[1])


def test_rejects_two_matches_for_one_candidacy():
    """`resolve` forces at most one mandate per candidacy, so two matches would make
    the published link depend on row order."""
    second = FILE.split("decisions:")[1].replace('"204521"', '"999999"')
    with pytest.raises(DecisionFileError, match="só pode ser vinculada"):
        parse_decisions(FILE + second)


def test_applies_to_the_queue_and_is_idempotent(session):
    _seed(session)
    decisions = parse_decisions(FILE)

    first = apply_decisions(session, decisions)
    session.commit()
    assert (first.applied, first.unchanged, first.skipped) == (1, 0, 0)

    row = session.query(ReviewQueue).one()
    assert row.status is ReviewStatus.match
    assert row.decided_by == "natanael"
    assert row.decided_at is not None

    # Re-applying an unchanged file must touch nothing — the build runs daily.
    second = apply_decisions(session, decisions)
    assert (second.applied, second.unchanged) == (0, 1)


def test_survives_a_rebuild_that_regenerates_every_uuid(session):
    """The point of the natural key. A cold rebuild gives the mandate and the queue
    row brand-new uuids; the same file must still land on the same pair."""
    _seed(session)
    decisions = parse_decisions(FILE)
    apply_decisions(session, decisions)
    session.commit()

    # Simulate the rebuild: drop the queue and the mandate, re-create them exactly as
    # the collectors would, with fresh uuids.
    old_mandate_id = session.query(ReviewQueue).one().mandate_id
    session.query(ReviewQueue).delete()
    session.query(Mandate).delete()
    session.commit()
    rebuilt = Mandate(
        house=House.CAMARA, house_member_id="204521", id_legislatura=57,
        sigla_uf="SC", nome_parlamentar="JOSE",
    )
    session.add(rebuilt)
    session.flush()
    session.add(
        ReviewQueue(sq_candidato="C1", mandate_id=rebuilt.id, status=ReviewStatus.pending)
    )
    session.commit()
    assert rebuilt.id != old_mandate_id

    result = apply_decisions(session, decisions)
    session.commit()

    assert result.applied == 1
    assert session.query(ReviewQueue).one().status is ReviewStatus.match


def test_reports_pairs_the_pipeline_never_proposed(session):
    """`apply` must not invent a queue entry: forcing a link nobody proposed would
    publish a human's guess as if the pipeline had found it."""
    _seed(session)
    session.query(ReviewQueue).delete()
    session.commit()

    result = apply_decisions(session, parse_decisions(FILE))

    assert result.applied == 0
    assert result.missing_queue == ["C1+CAMARA/204521"]
    assert session.query(ReviewQueue).count() == 0


def test_reports_decisions_whose_mandate_is_not_collected_yet(session):
    _seed(session)
    session.query(ReviewQueue).delete()
    session.query(Mandate).delete()
    session.commit()

    result = apply_decisions(session, parse_decisions(FILE))

    assert result.missing_mandate == ["C1+CAMARA/204521"]


def test_export_round_trips_through_the_parser(session):
    """What `export` emits must be something `apply` can read back — otherwise the
    documented workflow (export, decide, apply) is broken."""
    _seed(session)

    exported = export_pending(session)
    decisions = parse_decisions(exported.replace("decision: uncertain", "decision: match"))

    assert len(decisions) == 1
    assert decisions[0].sq_candidato == "C1"
    assert decisions[0].house is House.CAMARA
    assert decisions[0].id_legislatura == 57


def test_export_of_an_empty_queue_is_still_valid(session):
    _seed(session, status=ReviewStatus.match)  # nada pendente
    assert parse_decisions(export_pending(session)) == []
