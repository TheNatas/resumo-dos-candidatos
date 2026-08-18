from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from resumo.api.main import app
from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    ConfidenceTier,
    Expense,
    House,
    Mandate,
    MatchMethod,
    Person,
    Vote,
)

client = TestClient(app)


def _seed_incumbent(session):
    person = Person(cpf="12345678909", nome_normalizado="JOSE DA SILVA", nome_civil="JOSE DA SILVA")
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=House.CAMARA, house_member_id="1", id_legislatura=57, person_id=person.id,
        sigla_uf="SC", nome_parlamentar="JOSE", data_fim=None,
    )
    session.add(mandate)
    session.flush()
    session.add(
        Candidacy(
            sq_candidato="C1", ano_eleicao=2026, sg_uf="SC", cd_cargo=6, ds_cargo="DEPUTADO FEDERAL",
            nome_candidato="JOSE DA SILVA", nome_urna="JOSE", nome_normalizado="JOSE DA SILVA",
            sg_partido="PT", is_majoritario=False,
        )
    )
    # an unlinked candidacy
    session.add(
        Candidacy(
            sq_candidato="C2", ano_eleicao=2026, sg_uf="SC", cd_cargo=6, ds_cargo="DEPUTADO FEDERAL",
            nome_candidato="ANA PEREIRA", nome_normalizado="ANA PEREIRA", sg_partido="PSDB",
        )
    )
    session.add(
        CandidateMandateLink(
            sq_candidato="C1", mandate_id=mandate.id, person_id=person.id,
            match_method=MatchMethod.cpf_exact, confidence_score=1.0,
            confidence_tier=ConfidenceTier.auto_strong, is_incumbent_reelection=True,
            pipeline_version="test",
        )
    )
    session.add_all(
        [
            Vote(mandate_id=mandate.id, house_member_id="1", id_votacao="V1", tipo_voto="Sim",
                 data_votacao=dt.date(2024, 3, 1)),
            Vote(mandate_id=mandate.id, house_member_id="1", id_votacao="V2", tipo_voto="Não",
                 data_votacao=dt.date(2024, 3, 2)),
            Expense(mandate_id=mandate.id, house_member_id="1", ano=2024, mes=3,
                    valor_liquido=100.0, cod_documento="D1", num_documento="N1", row_hash="h1"),
        ]
    )
    session.commit()


def test_search_finds_candidate(session):
    _seed_incumbent(session)
    resp = client.get("/api/candidates", params={"q": "jose"})
    assert resp.status_code == 200
    assert any(c["sq_candidato"] == "C1" for c in resp.json())


def test_ficha_shows_track_record_only_for_confirmed_incumbent(session):
    _seed_incumbent(session)

    confirmed = client.get("/api/candidates/C1").json()
    assert confirmed["incumbent_confirmed"] is True
    assert confirmed["link"]["match_method"] == "cpf_exact"
    assert confirmed["track_record"]["votes_total"] == 2
    assert confirmed["track_record"]["votes_sim"] == 1
    assert confirmed["track_record"]["expense_total"] == 100.0

    unconfirmed = client.get("/api/candidates/C2").json()
    assert unconfirmed["incumbent_confirmed"] is False
    assert unconfirmed["track_record"] is None


def test_html_pages_render(session):
    _seed_incumbent(session)
    assert client.get("/").status_code == 200
    page = client.get("/candidato/C1")
    assert page.status_code == 200
    assert "Histórico de atuação" in page.text
    # unconfirmed shows the guard text, not a guessed link
    assert "Incumbência não confirmada" in client.get("/candidato/C2").text
