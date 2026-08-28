"""Scope: geographic (UF) and office (cargo) narrowing, and the taxonomy behind it.

These cover the seam that makes this a *Santa Catarina* platform rather than a
national one — including the failure mode that scoping introduces: an artifact whose
bytes are unchanged must still be re-ingested when the SCOPE widens.
"""

from __future__ import annotations

from sqlalchemy import func, select
from tests.helpers import make_tse_zip, tse_row

from resumo import cargos
from resumo.db.models import Candidacy
from resumo.ingestion.tse import parsing
from resumo.ingestion.tse.consulta_cand import ConsultaCandCollector


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ── cargo taxonomy ───────────────────────────────────────────────────────────
def test_senador_is_majoritario_but_files_no_proposta():
    """The distinction the old code conflated: majoritarian != files a proposta."""
    assert cargos.is_majoritario(cargos.SENADOR) is True
    assert cargos.requires_proposta(cargos.SENADOR) is False

    assert cargos.is_majoritario(cargos.GOVERNADOR) is True
    assert cargos.requires_proposta(cargos.GOVERNADOR) is True

    assert cargos.is_majoritario(cargos.DEPUTADO_FEDERAL) is False
    assert cargos.requires_proposta(cargos.DEPUTADO_FEDERAL) is False


def test_history_availability_distinguishes_the_three_reasons():
    A = cargos.HistoryAvailability
    # A source exists and is collected.
    assert cargos.history_availability(cargos.DEPUTADO_FEDERAL) is A.available
    assert cargos.history_availability(cargos.SENADOR) is A.available
    # ALESC: collected, but ~96% of its votes are simbólicas (no individual
    # position recorded) and there is nothing before Feb 2023.
    assert cargos.history_availability(cargos.DEPUTADO_ESTADUAL) is A.partial
    # An executive office with a collector: the acts a governor signs before the
    # Assembly (bills of executive initiative, vetoes) ARE published and collected.
    # `partial` because they are one real slice of governing and not the whole of it.
    assert cargos.history_availability(cargos.GOVERNADOR) is A.partial
    # The executive offices nothing reads stay not_applicable — "cargo executivo" is
    # no longer a synonym for "no record", so the distinction has to be asserted.
    assert cargos.history_availability(cargos.PREFEITO) is A.not_applicable
    assert cargos.history_availability(cargos.PRESIDENTE) is A.not_applicable
    # Municipal chambers really have no source in this platform.
    assert cargos.history_availability(cargos.VEREADOR) is A.no_public_source

    # Only the "available" case gets no explanatory note; `partial` keeps its
    # caveat precisely because data IS shown alongside it.
    assert cargos.history_note(cargos.DEPUTADO_FEDERAL) is None
    assert cargos.history_note(cargos.GOVERNADOR)
    assert cargos.history_note(cargos.DEPUTADO_ESTADUAL)

    # A track record is rendered for available AND partial, never for the other two.
    assert cargos.shows_track_record(cargos.DEPUTADO_FEDERAL) is True
    assert cargos.shows_track_record(cargos.DEPUTADO_ESTADUAL) is True
    assert cargos.shows_track_record(cargos.GOVERNADOR) is True
    assert cargos.shows_track_record(cargos.PREFEITO) is False
    assert cargos.shows_track_record(cargos.VEREADOR) is False


def test_parse_cargos_accepts_codes_and_names():
    assert cargos.parse_cargos("3,5,6,7") == frozenset({3, 5, 6, 7})
    assert cargos.parse_cargos("SENADOR, DEPUTADO FEDERAL") == frozenset({5, 6})
    assert cargos.parse_cargos("") == frozenset()  # empty spec = no filter
    assert cargos.parse_cargos(None) == frozenset()


# ── parsing-level UF narrowing ───────────────────────────────────────────────
def test_member_uf_ignores_the_consolidated_brasil_file():
    assert parsing.member_uf("consulta_cand_2026_SC.csv") == "SC"
    assert parsing.member_uf("consulta_cand_2026_BRASIL.csv") is None


def test_uf_filter_selects_only_the_matching_member_file(tmp_path):
    """The whole point of narrowing at the filename level: the other state's CSV is
    never decoded, not merely discarded after parsing."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for uf in ("SC", "SP"):
            csv_bytes = make_tse_zip([tse_row(SQ_CANDIDATO=f"{uf}1", SG_UF=uf)], member="x.csv")
            with zipfile.ZipFile(io.BytesIO(csv_bytes)) as inner:
                zf.writestr(f"consulta_cand_2026_{uf}.csv", inner.read("x.csv"))

    rows = list(parsing.iter_records(buf.getvalue(), ufs=["SC"]))
    assert {r["SQ_CANDIDATO"] for r in rows} == {"SC1"}
    assert all(r["__source_file"].endswith("_SC.csv") for r in rows)


def test_row_level_guard_narrows_a_consolidated_file(tmp_path):
    """When only a BRASIL file exists there is no per-UF member to pick, so the
    SG_UF guard has to do the narrowing."""
    data = make_tse_zip(
        [tse_row(SQ_CANDIDATO="A", SG_UF="SC"), tse_row(SQ_CANDIDATO="B", SG_UF="SP")],
        member="consulta_cand_2026_BRASIL.csv",
    )
    rows = list(parsing.iter_records(data, ufs=["SC"]))
    assert {r["SQ_CANDIDATO"] for r in rows} == {"A"}


def test_missing_sg_uf_column_does_not_drop_every_row():
    """A product that carries no SG_UF column at all must not be silently emptied by
    the geographic guard — absence of the column means "cannot filter", not "no match".
    """
    import io

    csv_text = "NOME;VALOR\nfulano;1\nsicrano;2\n"
    rows = list(parsing._csv_rows(io.StringIO(csv_text), "x.csv", frozenset({"SC"})))
    assert [r["NOME"] for r in rows] == ["fulano", "sicrano"]


def test_row_guard_drops_only_non_matching_states():
    import io

    csv_text = "SG_UF;NOME\nSC;a\nSP;b\nSC;c\n"
    rows = list(parsing._csv_rows(io.StringIO(csv_text), "x.csv", frozenset({"SC"})))
    assert [r["NOME"] for r in rows] == ["a", "c"]


# ── collector-level narrowing ────────────────────────────────────────────────
def test_collector_drops_out_of_scope_uf_and_cargo(tmp_path, session):
    rows = [
        tse_row(SQ_CANDIDATO="1", SG_UF="SC", CD_CARGO="6", DS_CARGO="DEPUTADO FEDERAL"),
        tse_row(SQ_CANDIDATO="2", SG_UF="SC", CD_CARGO="5", DS_CARGO="SENADOR"),
        tse_row(SQ_CANDIDATO="3", SG_UF="SC", CD_CARGO="13", DS_CARGO="VEREADOR"),  # out of cargo
        tse_row(SQ_CANDIDATO="4", SG_UF="SP", CD_CARGO="6", DS_CARGO="DEPUTADO FEDERAL"),  # out of UF
    ]
    src = _write(tmp_path, "consulta_cand_2026_BRASIL.zip",
                 make_tse_zip(rows, member="consulta_cand_2026_BRASIL.csv"))

    ConsultaCandCollector().run(session, source=src, year=2026, ufs=["SC"], cargo_codes=[3, 5, 6, 7])
    session.commit()

    kept = {sq for (sq,) in session.execute(select(Candidacy.sq_candidato))}
    assert kept == {"1", "2"}


def test_widening_the_scope_reingests_the_same_artifact(tmp_path, session):
    """Regression guard. Idempotency keys on (source_url, content_hash); with a scope
    filter the SAME bytes legitimately yield MORE rows, so a widened run must not be
    skipped as 'unchanged'."""
    rows = [
        tse_row(SQ_CANDIDATO="1", SG_UF="SC", CD_CARGO="6", DS_CARGO="DEPUTADO FEDERAL"),
        tse_row(SQ_CANDIDATO="2", SG_UF="SC", CD_CARGO="13", DS_CARGO="VEREADOR"),
    ]
    src = _write(tmp_path, "c.zip", make_tse_zip(rows))

    narrow = ConsultaCandCollector().run(session, source=src, year=2026, cargo_codes=[6])
    session.commit()
    assert narrow.status == "ingested"
    assert session.scalar(select(func.count()).select_from(Candidacy)) == 1

    # Same bytes, same URL — but a wider cargo scope. Must re-ingest, not skip.
    wide = ConsultaCandCollector().run(session, source=src, year=2026, cargo_codes=[6, 13])
    session.commit()
    assert wide.status == "ingested"
    assert session.scalar(select(func.count()).select_from(Candidacy)) == 2

    # Re-running the *same* scope is still a no-op.
    again = ConsultaCandCollector().run(session, source=src, year=2026, cargo_codes=[6, 13])
    session.commit()
    assert again.status == "skipped"


def test_empty_scope_means_no_filter(tmp_path, session):
    rows = [
        tse_row(SQ_CANDIDATO="1", SG_UF="SC", CD_CARGO="6"),
        tse_row(SQ_CANDIDATO="2", SG_UF="SP", CD_CARGO="13"),
    ]
    src = _write(tmp_path, "c.zip", make_tse_zip(rows, member="consulta_cand_2026_BRASIL.csv"))

    ConsultaCandCollector().run(session, source=src, year=2026, ufs=[], cargo_codes=[])
    session.commit()
    assert session.scalar(select(func.count()).select_from(Candidacy)) == 2
