from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.helpers import TINY_JPEG, make_foto_zip, make_proposta_zip, make_tse_zip, tse_row

from resumo.config import get_settings
from resumo.db.models import (
    Candidacy,
    CandidatePhoto,
    Coalition,
    GovernmentProposal,
    RawIngestion,
)
from resumo.ingestion.tse.consulta_cand import ConsultaCandCollector
from resumo.ingestion.tse.foto_candidato import FotoCandidatoCollector
from resumo.ingestion.tse.proposta_governo import PropostaGovernoCollector


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


@pytest.fixture
def _storage(tmp_path, monkeypatch):
    """Point the storage root at a temp dir; settings are cached, so clear it."""
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setenv("RESUMO_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


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


def _seed_candidate(session, tmp_path, sq: str, name: str = "c.zip") -> None:
    ConsultaCandCollector().run(
        session, source=_write(tmp_path, name, make_tse_zip([tse_row(SQ_CANDIDATO=sq)])), year=2022
    )
    session.commit()


def test_foto_maps_image_to_candidate(tmp_path, session, _storage):
    sq = "250001607903"
    _seed_candidate(session, tmp_path, sq)

    src = _write(
        tmp_path,
        "foto_cand2022_SC_div.zip",
        make_foto_zip({f"FSC{sq}_div.jpg": TINY_JPEG}),
    )
    res = FotoCandidatoCollector().run(session, source=src, year=2022, uf="SC")
    session.commit()

    assert res.status == "ingested" and res.row_count == 1
    photo = session.get(CandidatePhoto, sq)
    assert photo is not None
    assert photo.source == "tse_bulk_foto"
    assert photo.media_type == "image/jpeg"
    # The stored file is the bytes from the zip, not a re-encode.
    assert Path(photo.storage_path).read_bytes() == TINY_JPEG


def test_foto_leaves_unknown_candidates_unattributed(tmp_path, session, _storage):
    """A UF bundle carries every candidate in the state, while this install is scoped
    to four offices. Photos with no candidacy in base must be counted and dropped —
    never attached to whichever candidacy happens to be nearby."""
    sq = "250001607903"
    _seed_candidate(session, tmp_path, sq)

    src = _write(
        tmp_path,
        "foto_cand2022_SC_div.zip",
        make_foto_zip(
            {
                f"FSC{sq}_div.jpg": TINY_JPEG,
                "FSC250009999999_div.jpg": TINY_JPEG,  # candidatura fora do escopo
                "FSC_sem_digitos_div.jpg": TINY_JPEG,  # nome sem SQ algum
            }
        ),
    )
    res = FotoCandidatoCollector().run(session, source=src, year=2022, uf="SC")
    session.commit()

    assert res.row_count == 1
    assert "2 fotos sem candidatura" in (res.detail or "")
    assert session.scalar(select(func.count()).select_from(CandidatePhoto)) == 1


def test_foto_replaces_rather_than_accumulates(tmp_path, session, _storage):
    """A candidacy has ONE registration photo. A re-issued image must replace the
    old row, not sit beside it — a second row would leave the page choosing a face."""
    sq = "250001607903"
    _seed_candidate(session, tmp_path, sq)

    first = _write(tmp_path, "f1.zip", make_foto_zip({f"FSC{sq}_div.jpg": TINY_JPEG}))
    FotoCandidatoCollector().run(session, source=first, year=2022, uf="SC")
    session.commit()

    reissued = TINY_JPEG + b"\x00"
    second = _write(tmp_path, "f2.zip", make_foto_zip({f"FSC{sq}_div.jpg": reissued}))
    FotoCandidatoCollector().run(session, source=second, year=2022, uf="SC")
    session.commit()

    assert session.scalar(select(func.count()).select_from(CandidatePhoto)) == 1
    assert Path(session.get(CandidatePhoto, sq).storage_path).read_bytes() == reissued


def test_foto_is_idempotent(tmp_path, session, _storage):
    sq = "250001607903"
    _seed_candidate(session, tmp_path, sq)
    src = _write(tmp_path, "f.zip", make_foto_zip({f"FSC{sq}_div.jpg": TINY_JPEG}))

    first = FotoCandidatoCollector().run(session, source=src, year=2022, uf="SC")
    session.commit()
    second = FotoCandidatoCollector().run(session, source=src, year=2022, uf="SC")
    session.commit()

    assert first.status == "ingested"
    assert second.status == "skipped"
