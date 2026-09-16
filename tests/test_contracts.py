"""Contracts shared by the live API and generated static payloads."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from resumo.api.main import app
from resumo.db.models import Candidacy
from resumo.render import render_site

client = TestClient(app)


def _seed(session):
    session.add(
        Candidacy(
            sq_candidato="CONTRACT1",
            ano_eleicao=2026,
            sg_uf="SC",
            cd_cargo=3,
            ds_cargo="GOVERNADOR",
            nome_candidato="MARIA CONTRATO",
            nome_urna="MARIA",
            nome_normalizado="MARIA CONTRATO",
            nr_candidato="1313",
            sg_partido="PT",
            is_majoritario=True,
        )
    )
    session.commit()


def test_api_exposes_stable_summary_and_detail_contract(session):
    _seed(session)

    summaries = client.get("/api/candidates").json()
    assert len(summaries) == 1
    assert set(summaries[0]) == {
        "sq_candidato",
        "nome",
        "nome_urna",
        "numero",
        "foto_url",
        "iniciais",
        "nome_normalizado",
        "ano",
        "cd_cargo",
        "cargo",
        "uf",
        "partido",
        "situacao",
        "incumbent_confirmed",
        "incumbent_house",
        "reelection_same_office",
        "resultado",
        "majoritario",
        "requires_proposta",
        "history_status",
        "history_note",
    }
    assert summaries[0]["sq_candidato"] == "CONTRACT1"
    assert summaries[0]["incumbent_confirmed"] is False

    detail = client.get("/api/candidates/CONTRACT1")
    assert detail.status_code == 200
    assert set(detail.json()) == {
        "candidacy",
        "photo",
        "social_links",
        "proposals",
        "incumbent_confirmed",
        "link",
        "track_record",
        "amendments",
        "campaign_finance",
        "top_donors",
    }


def test_static_payloads_match_live_api(session, tmp_path, monkeypatch):
    _seed(session)
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("RESUMO_STORAGE_DIR", str(storage))
    from resumo.config import get_settings

    get_settings.cache_clear()
    try:
        out = tmp_path / "site"
        render_site(session, out=out, base_url="", site_url=None)

        live_summary = client.get("/api/candidates").json()
        static_summary = json.loads((out / "api" / "candidates.json").read_text())
        assert static_summary == live_summary

        live_detail = client.get("/api/candidates/CONTRACT1").json()
        static_detail = json.loads(
            (out / "api" / "candidates" / "CONTRACT1.json").read_text()
        )
        assert static_detail == live_detail
    finally:
        get_settings.cache_clear()