from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from resumo.api.main import app
from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    ConfidenceTier,
    Expense,
    GovernmentProposal,
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


def _seed_other_year(session):
    """A row from the historical validation set. The same database holds ~29k of
    these; the public surface must not list them under the current election."""
    session.add(
        Candidacy(
            sq_candidato="OLD1", ano_eleicao=2022, sg_uf="SC", cd_cargo=6,
            ds_cargo="DEPUTADO FEDERAL", nome_candidato="JOSE ANTIGO",
            nome_normalizado="JOSE ANTIGO", sg_partido="PT",
            ds_sit_tot_turno="ELEITO",
        )
    )
    session.commit()


def test_search_excludes_other_election_years(session):
    _seed_incumbent(session)
    _seed_other_year(session)

    # Both rows match "jose"; only the configured year may come back.
    sqs = [c["sq_candidato"] for c in client.get("/api/candidates", params={"q": "jose"}).json()]
    assert "C1" in sqs
    assert "OLD1" not in sqs

    html = client.get("/", params={"q": "jose"}).text
    assert "JOSE ANTIGO" not in html

    # Still reachable when asked for explicitly — auditing 2026 against 2022 is a
    # legitimate use; an accidentally unscoped listing is not.
    explicit = client.get("/api/candidates", params={"q": "jose", "year": 2022}).json()
    assert [c["sq_candidato"] for c in explicit] == ["OLD1"]


def test_proposal_is_downloadable_and_hides_storage_path(session, tmp_path, monkeypatch):
    _seed_incumbent(session)
    storage = tmp_path / "storage"
    (storage / "proposta" / "2026").mkdir(parents=True)
    pdf = storage / "proposta" / "2026" / "proposta.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    proposal = GovernmentProposal(
        sq_candidato="C1", source="tse_bulk_pdf", storage_path=str(pdf),
        original_filename="proposta.pdf", content_hash="deadbeef",
    )
    session.add(proposal)
    session.commit()

    detail = client.get("/api/candidates/C1").json()
    (published,) = detail["proposals"]
    # The server filesystem path is not part of the public payload.
    assert "storage_path" not in published
    assert published["url"] == f"/proposta/{proposal.id}.pdf"

    # And the ficha actually links to it — collecting the PDF is pointless if no
    # reader can open it.
    assert published["url"] in client.get("/candidato/C1").text

    monkeypatch.setenv("RESUMO_STORAGE_DIR", str(storage))
    from resumo.config import get_settings

    get_settings.cache_clear()
    try:
        resp = client.get(published["url"])
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-1.4 fake"
    finally:
        get_settings.cache_clear()


def test_proposal_outside_storage_root_is_not_served(session, tmp_path, monkeypatch):
    """storage_path is a filesystem path read from a database row; a row pointing
    outside the storage root must not become an arbitrary-file read."""
    _seed_incumbent(session)
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")
    proposal = GovernmentProposal(
        sq_candidato="C1", source="tse_bulk_pdf", storage_path=str(outside),
        original_filename="secret.pdf", content_hash="cafe",
    )
    session.add(proposal)
    session.commit()

    monkeypatch.setenv("RESUMO_STORAGE_DIR", str(tmp_path / "storage"))
    from resumo.config import get_settings

    get_settings.cache_clear()
    try:
        assert client.get(f"/proposta/{proposal.id}.pdf").status_code == 404
    finally:
        get_settings.cache_clear()


def test_about_page_is_served_live_too(session):
    resp = client.get("/sobre")
    assert resp.status_code == 200
    assert "votação simbólica" in resp.text
    # O rodapé aponta com barra final; o app redireciona em vez de 404.
    assert client.get("/sobre/", follow_redirects=True).status_code == 200
