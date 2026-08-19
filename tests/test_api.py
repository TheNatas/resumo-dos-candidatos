from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from resumo.api.main import app
from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    CandidatePhoto,
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


def test_page_filters_by_partido(session):
    _seed_incumbent(session)
    # Out of scope (other election year): its sigla must not reach the filter.
    session.add(
        Candidacy(
            sq_candidato="OLD9", ano_eleicao=2022, sg_uf="SC", cd_cargo=6,
            ds_cargo="DEPUTADO FEDERAL", nome_candidato="PEDRO ANTIGO",
            nome_normalizado="PEDRO ANTIGO", sg_partido="PSOL",
        )
    )
    session.commit()

    page = client.get("/").text
    assert '<option value="PT"' in page
    assert '<option value="PSDB"' in page
    assert '<option value="PSOL"' not in page

    filtered = client.get("/", params={"partido": "PT"}).text
    assert "JOSE" in filtered
    assert "ANA PEREIRA" not in filtered


def test_api_filters_by_partido(session):
    _seed_incumbent(session)
    # Lowercase in, exact sigla out: the sigla is normalized, never substring-matched.
    listed = client.get("/api/candidates", params={"partido": "pt"}).json()
    assert [c["sq_candidato"] for c in listed] == ["C1"]


def test_filters_by_reeleicao(session):
    _seed_incumbent(session)
    # A review-tier link is exactly what must NOT count as a confirmed re-election.
    session.add(
        CandidateMandateLink(
            sq_candidato="C2", mandate_id=session.query(Mandate).one().id,
            person_id=session.query(Person).one().id,
            match_method=MatchMethod.probabilistic, confidence_score=0.5,
            confidence_tier=ConfidenceTier.review, is_incumbent_reelection=True,
            pipeline_version="test",
        )
    )
    session.commit()

    confirmed = client.get("/api/candidates", params={"reeleicao": "true"}).json()
    assert [c["sq_candidato"] for c in confirmed] == ["C1"]
    assert confirmed[0]["incumbent_confirmed"] is True

    rest = client.get("/api/candidates", params={"reeleicao": "false"}).json()
    assert [c["sq_candidato"] for c in rest] == ["C2"]
    assert rest[0]["incumbent_confirmed"] is False

    page = client.get("/", params={"reeleicao": "sim"}).text
    assert "JOSE" in page
    assert "ANA PEREIRA" not in page
    # An empty select submits an empty string; it must read as "no filter", not 422.
    assert client.get("/", params={"reeleicao": ""}).status_code == 200


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


def _majoritario(sq: str, nome: str, *, partido: str, uf: str = "SC", ano: int = 2026) -> Candidacy:
    return Candidacy(
        sq_candidato=sq, ano_eleicao=ano, sg_uf=uf, cd_cargo=3, ds_cargo="GOVERNADOR",
        nome_candidato=nome, nome_urna=nome, nome_normalizado=nome, sg_partido=partido,
        is_majoritario=True,
    )


def _proposal(sq: str, filename: str, content_hash: str) -> GovernmentProposal:
    return GovernmentProposal(
        sq_candidato=sq, source="tse_bulk_pdf", storage_path=None,
        original_filename=filename, content_hash=content_hash,
    )


def test_proposal_filed_by_several_candidacies_is_flagged_as_the_party_s(session):
    """The TSE zip only says which candidate a PDF was filed under — never whether the
    text is the party's program. The same file under two candidacies is the one signal
    that survives without reading it, and the reader gets it before the click."""
    session.add_all(
        [
            _majoritario("P1", "ANA", partido="PT"),
            _majoritario("P2", "BRUNO", partido="PT", uf="PR"),
            # Same person re-filing the same platform four years earlier: a different
            # sq_candidato, but nobody is sharing anything with anybody.
            _majoritario("P0", "ANA", partido="PT", ano=2022),
        ]
    )
    session.add_all(
        [
            _proposal("P1", "programa-pt.pdf", "shared"),
            _proposal("P2", "programa-pt.pdf", "shared"),
            _proposal("P0", "programa-pt.pdf", "shared"),
            _proposal("P1", "plano-ana.pdf", "own"),
        ]
    )
    session.commit()

    detail = client.get("/api/candidates/P1").json()
    by_name = {p["filename"]: p for p in detail["proposals"]}

    party = by_name["programa-pt.pdf"]
    assert party["scope"] == "party"
    assert party["scope_label"] == "Documento do partido"
    assert party["shared_with"] == 1  # the 2022 filing is not counted
    assert "2 candidaturas do PT" in party["scope_note"]

    own = by_name["plano-ana.pdf"]
    assert own["scope"] == "candidacy"
    assert own["scope_label"] is None and own["scope_note"] is None

    # The flag is on the ficha, next to the link, not only in the JSON.
    page = client.get("/candidato/P1").text
    assert "Documento do partido" in page


def test_proposal_shared_across_parties_is_not_called_the_party_s(session):
    """Two parties behind the same file means it is not this candidate's — but calling
    it a coligação would be an inference the data does not carry."""
    session.add_all(
        [_majoritario("Q1", "CARLA", partido="PL"), _majoritario("Q2", "DINO", partido="PP")]
    )
    session.add_all(
        [_proposal("Q1", "programa.pdf", "dupla"), _proposal("Q2", "programa.pdf", "dupla")]
    )
    session.commit()

    (proposal,) = client.get("/api/candidates/Q1").json()["proposals"]
    assert proposal["scope"] == "shared"
    assert proposal["scope_label"] == "Documento compartilhado"
    assert "(PL, PP)" in proposal["scope_note"]


def test_photo_route_serves_the_stored_file(session, tmp_path, monkeypatch):
    from tests.helpers import TINY_JPEG

    from resumo.config import get_settings

    root = tmp_path / "storage"
    (root / "foto" / "2026").mkdir(parents=True)
    monkeypatch.setenv("RESUMO_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    try:
        _seed_incumbent(session)
        jpg = root / "foto" / "2026" / "C1_abc.jpg"
        jpg.write_bytes(TINY_JPEG)
        session.add(
            CandidatePhoto(
                sq_candidato="C1", source="tse_bulk_foto", storage_path=str(jpg),
                media_type="image/jpeg", content_hash="abc",
            )
        )
        # C2 has a row pointing outside the storage root: the route must refuse it
        # rather than turn a database row into an arbitrary-file read.
        outside = tmp_path / "secret.jpg"
        outside.write_bytes(b"segredo")
        session.add(
            CandidatePhoto(
                sq_candidato="C2", source="tse_bulk_foto", storage_path=str(outside),
                media_type="image/jpeg", content_hash="def",
            )
        )
        session.commit()

        ok = client.get("/foto/C1.jpg")
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "image/jpeg"
        assert ok.content == TINY_JPEG

        assert client.get("/foto/C2.jpg").status_code == 404
        assert client.get("/foto/NAOEXISTE.jpg").status_code == 404
    finally:
        get_settings.cache_clear()


def test_detail_reports_a_missing_photo_as_null(session):
    _seed_incumbent(session)
    detail = client.get("/api/candidates/C1").json()
    assert detail["photo"] is None
    assert detail["candidacy"]["foto_url"] is None
    # As iniciais existem sempre — é o que a página desenha no lugar da foto.
    assert detail["candidacy"]["iniciais"] == "JS"
