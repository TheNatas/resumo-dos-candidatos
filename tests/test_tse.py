from __future__ import annotations

from sqlalchemy import func, select
from tests.helpers import make_proposta_zip, make_tse_zip, tse_row

from resumo.db.models import Candidacy, Coalition, GovernmentProposal, RawIngestion
from resumo.ingestion.tse.consulta_cand import ConsultaCandCollector
from resumo.ingestion.tse.proposta_governo import PropostaGovernoCollector


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_consulta_cand_ingests_and_derives_coalition(tmp_path, session):
    rows = [
        tse_row(SQ_CANDIDATO="250000111", NM_CANDIDATO="JOSÉ DA SILVA", NR_CPF_CANDIDATO="123.456.789-09",
                SQ_COLIGACAO="COL1", NM_COLIGACAO="COLIGACAO A", DS_COMPOSICAO_COLIGACAO="PT / PSB"),
        tse_row(SQ_CANDIDATO="250000222", NM_CANDIDATO="MARIA SOUZA", CD_CARGO="3", DS_CARGO="GOVERNADOR"),
    ]
    src = _write(tmp_path, "consulta_cand_2022.zip", make_tse_zip(rows))

    res = ConsultaCandCollector().run(session, source=src, year=2022)
    session.commit()

    assert res.status == "ingested"
    assert session.scalar(select(func.count()).select_from(Candidacy)) == 2
    assert session.scalar(select(func.count()).select_from(Coalition)) == 1
    jose = session.get(Candidacy, "250000111")
    assert jose.nome_normalizado == "JOSE DA SILVA"  # latin-1 decoded + normalized
    assert jose.cpf_raw == "123.456.789-09"
    gov = session.get(Candidacy, "250000222")
    assert gov.is_majoritario is True  # GOVERNADOR -> majoritário


def test_consulta_cand_is_idempotent(tmp_path, session):
    src = _write(tmp_path, "consulta_cand_2022.zip", make_tse_zip([tse_row(SQ_CANDIDATO="1")]))

    first = ConsultaCandCollector().run(session, source=src, year=2022)
    session.commit()
    second = ConsultaCandCollector().run(session, source=src, year=2022)
    session.commit()

    assert first.status == "ingested"
    assert second.status == "skipped"  # same hash -> no-op
    assert session.scalar(select(func.count()).select_from(Candidacy)) == 1
    # exactly one successful ingestion recorded
    assert session.scalar(
        select(func.count()).select_from(RawIngestion).where(RawIngestion.status == "success")
    ) == 1


def test_proposta_maps_pdf_to_candidate(tmp_path, session):
    # candidate must exist first (FK); real SQ_CANDIDATO are ~12 digits
    sq = "250001607903"
    ConsultaCandCollector().run(
        session, source=_write(tmp_path, "c.zip", make_tse_zip([tse_row(SQ_CANDIDATO=sq)])), year=2022
    )
    session.commit()

    pdf_member = f"PROPOSTA_GOVERNO/{sq}-proposta.pdf"
    src = _write(tmp_path, "proposta_governo_2022_SC.zip", make_proposta_zip(pdf_member))
    res = PropostaGovernoCollector().run(session, source=src, year=2022)
    session.commit()

    assert res.status == "ingested"
    prop = session.scalar(select(GovernmentProposal).where(GovernmentProposal.sq_candidato == sq))
    assert prop is not None and prop.content_hash
