"""Prestação de contas eleitorais: the four CSV families and the traps in them.

All fixtures are synthetic (no network). The real 2022 zip is 473 MB; the 2026 one
was still header-only when this collector was written, which is precisely why the
"zero rows is a normal outcome" case is covered here.
"""

from __future__ import annotations

from sqlalchemy import func, select
from tests.helpers import (
    make_prestacao_contas_zip,
    make_tse_zip,
    pc_despesa_contratada_row,
    pc_despesa_paga_row,
    pc_doador_originario_row,
    pc_receita_row,
    tse_row,
)

from resumo.db.models import (
    AccountFiling,
    CampaignExpense,
    CampaignPayment,
    CampaignRevenue,
    CampaignRevenueOriginator,
)
from resumo.ingestion.tse.consulta_cand import ConsultaCandCollector
from resumo.ingestion.tse.prestacao_contas import PrestacaoContasCollector, cdn_url, parse_filing

SQ = "250001607903"  # real SQ_CANDIDATO are ~12 digits
PRESTADOR = "700001"


def _revenue(session, sq_receita: str) -> CampaignRevenue | None:
    """Fetch a receipt by SQ_RECEITA.

    Not `session.get`: SQ_RECEITA is deliberately NOT the primary key — the sequence
    repeats across genuinely different receipts, so identity is a row hash. Tests
    that seed one row per sequence can still look it up this way.
    """
    return session.execute(
        select(CampaignRevenue).where(CampaignRevenue.sq_receita == sq_receita)
    ).scalars().first()




def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _candidacy(tmp_path, session, sq: str = SQ) -> None:
    """Ingest the candidacy the finance rows will hang off (FK target)."""
    src = _write(tmp_path, f"cand_{sq}.zip", make_tse_zip([tse_row(SQ_CANDIDATO=sq)]))
    ConsultaCandCollector().run(session, source=src, year=2022)
    session.commit()


# ── URL construction ─────────────────────────────────────────────────────────
def test_cdn_url_uses_the_short_directory_and_the_long_file_stem():
    """The trap: directory `prestacao_contas`, file `prestacao_de_contas_...`."""
    url = cdn_url(2026)
    assert url.endswith(
        "/prestacao_contas/prestacao_de_contas_eleitorais_candidatos_2026.zip"
    )
    assert "/prestacao_de_contas/" not in url  # that directory 404s for every year


# ── 1. Receitas + contratadas link to an existing candidacy ──────────────────
def test_receitas_and_contratadas_link_to_the_candidacy(tmp_path, session):
    _candidacy(tmp_path, session)
    src = _write(
        tmp_path,
        "pc_2022.zip",
        make_prestacao_contas_zip(
            receitas=[
                pc_receita_row(SQ_RECEITA="R1", SQ_CANDIDATO=SQ, VR_RECEITA="10.000,00"),
                pc_receita_row(SQ_RECEITA="R2", SQ_CANDIDATO=SQ, VR_RECEITA="1.000.000,00"),
            ],
            contratadas=[
                pc_despesa_contratada_row(SQ_DESPESA="D1", SQ_CANDIDATO=SQ),
            ],
            originarios=[pc_doador_originario_row(SQ_RECEITA="R1")],
        ),
    )

    res = PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()

    assert res.status == "ingested"
    assert session.scalar(select(func.count()).select_from(CampaignRevenue)) == 2
    assert session.scalar(select(func.count()).select_from(CampaignExpense)) == 1

    r2 = _revenue(session, "R2")
    assert r2.sq_candidato == SQ
    assert float(r2.vr_receita) == 1_000_000.00  # "1.000.000,00" parsed, not truncated
    assert r2.ano_eleicao == 2022
    assert r2.st_turno == 1  # from ST_TURNO, not NR_TURNO
    assert r2.nm_doador_rfb == "PARTIDO DOS TRABALHADORES"

    exp = session.scalar(select(CampaignExpense))
    assert exp.sq_candidato == SQ
    assert exp.nm_fornecedor_rfb == "GRAFICA CENTRAL LTDA"
    assert float(exp.vr_despesa_contratada) == 5000.00

    # Originator kept, and attached to the receipt that was actually ingested.
    orig = session.scalar(select(CampaignRevenueOriginator))
    assert orig.sq_receita == "R1"
    assert orig.nr_cpf_cnpj_doador_originario == "11122233344"


def test_originators_without_an_ingested_receipt_are_dropped(tmp_path, session):
    """The FK on campaign_revenue.sq_receita must not be violated by a stray row."""
    _candidacy(tmp_path, session)
    src = _write(
        tmp_path,
        "pc_orphan_orig.zip",
        make_prestacao_contas_zip(
            receitas=[pc_receita_row(SQ_RECEITA="R1", SQ_CANDIDATO=SQ)],
            originarios=[
                pc_doador_originario_row(SQ_RECEITA="R1"),
                pc_doador_originario_row(SQ_RECEITA="GHOST"),  # no such receipt
            ],
        ),
    )

    PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()

    kept = {sq for (sq,) in session.execute(select(CampaignRevenueOriginator.sq_receita))}
    assert kept == {"R1"}


# ── 2. despesas_pagas resolves the candidate through the prestador map ───────
def test_despesas_pagas_resolve_candidate_via_prestador(tmp_path, session):
    """The paid-expenses family has NO candidate column: it must still be attributed."""
    _candidacy(tmp_path, session)
    src = _write(
        tmp_path,
        "pc_pagas.zip",
        make_prestacao_contas_zip(
            receitas=[
                pc_receita_row(SQ_RECEITA="R1", SQ_CANDIDATO=SQ, SQ_PRESTADOR_CONTAS=PRESTADOR)
            ],
            pagas=[
                pc_despesa_paga_row(SQ_DESPESA="D1", SQ_PRESTADOR_CONTAS=PRESTADOR),
                # A prestador nobody declared — kept (the money is real), unattributed.
                pc_despesa_paga_row(SQ_DESPESA="D2", SQ_PRESTADOR_CONTAS="999999"),
            ],
        ),
    )

    res = PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()

    assert res.status == "ingested"
    paid = {
        p.sq_despesa: p for p in session.scalars(select(CampaignPayment))
    }
    assert paid["D1"].sq_candidato == SQ
    assert float(paid["D1"].vr_pagto_despesa) == 2500.00  # VR_PAGTO_DESPESA
    assert paid["D2"].sq_candidato is None  # unresolved, but NOT dropped
    assert paid["D2"].sq_prestador_contas == "999999"
    assert "1 payments without a candidacy" in res.detail


# ── 3. TP_PRESTACAO_CONTAS maps case-insensitively ──────────────────────────
def test_filing_type_is_case_and_accent_insensitive():
    """receitas shouts ("FINAL"), despesas title-cases ("Final") — same enum."""
    assert parse_filing("FINAL") is AccountFiling.final
    assert parse_filing("Final") is AccountFiling.final
    assert parse_filing("Parcial") is AccountFiling.parcial
    assert parse_filing("PARCIAL") is AccountFiling.parcial
    assert parse_filing("Relatório Financeiro") is AccountFiling.relatorio_financeiro
    assert parse_filing("RELATÓRIO FINANCEIRO") is AccountFiling.relatorio_financeiro
    assert parse_filing("Regularização da Omissão") is AccountFiling.regularizacao_omissao
    assert parse_filing("#NULO#") is AccountFiling.outro
    assert parse_filing(None) is AccountFiling.outro


def test_filing_type_survives_the_casing_split_between_families(tmp_path, session):
    _candidacy(tmp_path, session)
    src = _write(
        tmp_path,
        "pc_filing.zip",
        make_prestacao_contas_zip(
            receitas=[
                pc_receita_row(SQ_RECEITA="R1", SQ_CANDIDATO=SQ, TP_PRESTACAO_CONTAS="FINAL"),
                pc_receita_row(SQ_RECEITA="R2", SQ_CANDIDATO=SQ, TP_PRESTACAO_CONTAS="PARCIAL"),
            ],
            contratadas=[
                pc_despesa_contratada_row(
                    SQ_DESPESA="D1", SQ_CANDIDATO=SQ, TP_PRESTACAO_CONTAS="Final"
                ),
                pc_despesa_contratada_row(
                    SQ_DESPESA="D2", SQ_CANDIDATO=SQ, TP_PRESTACAO_CONTAS="Relatório Financeiro"
                ),
            ],
        ),
    )

    PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()

    assert _revenue(session, "R1").tp_prestacao_contas is AccountFiling.final
    assert _revenue(session, "R2").tp_prestacao_contas is AccountFiling.parcial
    filings = {
        e.sq_despesa: e.tp_prestacao_contas for e in session.scalars(select(CampaignExpense))
    }
    assert filings["D1"] is AccountFiling.final
    assert filings["D2"] is AccountFiling.relatorio_financeiro
    # The FINAL file retains parciais: an aggregation that ignores the enum would
    # count R2 alongside R1.
    finals = session.scalar(
        select(func.count())
        .select_from(CampaignRevenue)
        .where(CampaignRevenue.tp_prestacao_contas == AccountFiling.final)
    )
    assert finals == 1


# ── 4. Repeated SQ_DESPESA -> distinct rows, and re-runs stay idempotent ─────
def test_repeated_sq_despesa_yields_distinct_rows_and_reruns_are_idempotent(tmp_path, session):
    """SQ_DESPESA repeats (installments / multi-line invoices), so identity is a row
    hash — several line items must survive as several rows, twice."""
    _candidacy(tmp_path, session)
    data = make_prestacao_contas_zip(
        receitas=[pc_receita_row(SQ_RECEITA="R1", SQ_CANDIDATO=SQ)],
        contratadas=[
            pc_despesa_contratada_row(SQ_DESPESA="D1", SQ_CANDIDATO=SQ, DS_DESPESA="Item A",
                                      VR_DESPESA_CONTRATADA="1.000,00"),
            pc_despesa_contratada_row(SQ_DESPESA="D1", SQ_CANDIDATO=SQ, DS_DESPESA="Item B",
                                      VR_DESPESA_CONTRATADA="2.000,00"),
            # Byte-identical to the previous line: still a second line item.
            pc_despesa_contratada_row(SQ_DESPESA="D1", SQ_CANDIDATO=SQ, DS_DESPESA="Item B",
                                      VR_DESPESA_CONTRATADA="2.000,00"),
        ],
        pagas=[
            pc_despesa_paga_row(SQ_DESPESA="D1", SQ_PARCELAMENTO_DESPESA="P1",
                                VR_PAGTO_DESPESA="1.500,00"),
            pc_despesa_paga_row(SQ_DESPESA="D1", SQ_PARCELAMENTO_DESPESA="P2",
                                VR_PAGTO_DESPESA="1.500,00"),
        ],
    )
    first = PrestacaoContasCollector().run(
        session, source=_write(tmp_path, "pc_a.zip", data), year=2022
    )
    session.commit()

    assert first.status == "ingested"
    assert session.scalar(select(func.count()).select_from(CampaignExpense)) == 3
    assert session.scalar(select(func.count()).select_from(CampaignPayment)) == 2
    # contratadas and pagas are many-to-many on sq_despesa: each side sums on its own.
    assert float(session.scalar(select(func.sum(CampaignExpense.vr_despesa_contratada)))) == 5000.0
    assert float(session.scalar(select(func.sum(CampaignPayment.vr_pagto_despesa)))) == 3000.0

    # Same bytes at the same path -> ledger no-op.
    again = PrestacaoContasCollector().run(
        session, source=_write(tmp_path, "pc_a.zip", data), year=2022
    )
    session.commit()
    assert again.status == "skipped"

    # Same bytes at a DIFFERENT path -> the ledger cannot help; the row hashes must.
    second = PrestacaoContasCollector().run(
        session, source=_write(tmp_path, "pc_b.zip", data), year=2022
    )
    session.commit()
    assert second.status == "ingested"
    assert session.scalar(select(func.count()).select_from(CampaignExpense)) == 3
    assert session.scalar(select(func.count()).select_from(CampaignPayment)) == 2
    assert session.scalar(select(func.count()).select_from(CampaignRevenue)) == 1


# ── 5. A header-only artifact is a normal outcome ───────────────────────────
def test_header_only_zip_is_empty_not_an_error(tmp_path, session):
    """As of Aug/2026 every per-UF file in the 2026 zip has 0 data rows. That is the
    expected state until the filing windows (parcial 9-13 Sep, finais 3 Nov)."""
    src = _write(tmp_path, "pc_2026.zip", make_prestacao_contas_zip(year=2026))

    res = PrestacaoContasCollector().run(session, source=src, year=2026)
    session.commit()

    assert res.status == "empty"
    assert res.row_count == 0
    assert session.scalar(select(func.count()).select_from(CampaignRevenue)) == 0
    assert session.scalar(select(func.count()).select_from(CampaignPayment)) == 0


# ── 6. Out-of-scope candidacies must not blow up the FK ─────────────────────
def test_rows_for_an_uningested_candidacy_do_not_violate_the_fk(tmp_path, session):
    """The zip is national; the candidacy table is UF/cargo-scoped. Rows pointing at
    a candidacy we never ingested keep their data with a NULL candidate reference."""
    _candidacy(tmp_path, session)
    src = _write(
        tmp_path,
        "pc_orphans.zip",
        make_prestacao_contas_zip(
            receitas=[
                pc_receita_row(SQ_RECEITA="R1", SQ_CANDIDATO=SQ, SQ_PRESTADOR_CONTAS=PRESTADOR),
                pc_receita_row(
                    SQ_RECEITA="R2", SQ_CANDIDATO="999999999999", SQ_PRESTADOR_CONTAS="700002"
                ),
            ],
            contratadas=[
                pc_despesa_contratada_row(SQ_DESPESA="D2", SQ_CANDIDATO="999999999999",
                                          SQ_PRESTADOR_CONTAS="700002"),
            ],
            pagas=[pc_despesa_paga_row(SQ_DESPESA="D2", SQ_PRESTADOR_CONTAS="700002")],
        ),
    )

    res = PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()

    assert res.status == "ingested"
    orphan = _revenue(session, "R2")
    assert orphan is not None
    assert orphan.sq_candidato is None  # nulled, not dropped
    assert orphan.sq_prestador_contas == "700002"  # provenance survives
    assert _revenue(session, "R1").sq_candidato == SQ
    # The payment resolved to 999999999999 through the prestador map, then had the
    # unknown reference nulled — it must still be stored.
    payment = session.scalar(select(CampaignPayment))
    assert payment.sq_candidato is None
    assert payment.sq_prestador_contas == "700002"


# ── Scope ───────────────────────────────────────────────────────────────────
def test_uf_scope_narrows_before_decoding(tmp_path, session):
    """One national zip, no per-UF zips: narrowing happens on the members inside."""
    import io
    import zipfile

    _candidacy(tmp_path, session)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for uf, sq_receita in (("SC", "R_SC"), ("SP", "R_SP")):
            inner = make_prestacao_contas_zip(
                receitas=[pc_receita_row(SQ_RECEITA=sq_receita, SQ_CANDIDATO=SQ, SG_UF=uf)],
                uf=uf,
            )
            with zipfile.ZipFile(io.BytesIO(inner)) as src_zip:
                for member in src_zip.namelist():
                    if member.endswith(".csv"):
                        zf.writestr(member, src_zip.read(member))

    src = _write(tmp_path, "pc_national.zip", buf.getvalue())
    PrestacaoContasCollector().run(session, source=src, year=2022, ufs=["SC"])
    session.commit()

    kept = {sq for (sq,) in session.execute(select(CampaignRevenue.sq_receita))}
    assert kept == {"R_SC"}


def test_widening_the_uf_scope_reingests_the_same_bytes(tmp_path, session):
    """Same artifact, wider scope -> more rows, so it must not be skipped as unchanged."""
    import io
    import zipfile

    _candidacy(tmp_path, session)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for uf, sq_receita in (("SC", "R_SC"), ("SP", "R_SP")):
            inner = make_prestacao_contas_zip(
                receitas=[pc_receita_row(SQ_RECEITA=sq_receita, SQ_CANDIDATO=SQ, SG_UF=uf)],
                uf=uf,
            )
            with zipfile.ZipFile(io.BytesIO(inner)) as src_zip:
                for member in src_zip.namelist():
                    if member.endswith(".csv"):
                        zf.writestr(member, src_zip.read(member))
    src = _write(tmp_path, "pc_scope.zip", buf.getvalue())

    narrow = PrestacaoContasCollector().run(session, source=src, year=2022, ufs=["SC"])
    session.commit()
    assert narrow.status == "ingested"
    assert session.scalar(select(func.count()).select_from(CampaignRevenue)) == 1

    wide = PrestacaoContasCollector().run(session, source=src, year=2022, ufs=["SC", "SP"])
    session.commit()
    assert wide.status == "ingested"
    assert session.scalar(select(func.count()).select_from(CampaignRevenue)) == 2

    again = PrestacaoContasCollector().run(session, source=src, year=2022, ufs=["SC", "SP"])
    session.commit()
    assert again.status == "skipped"


# ── Sentinels ───────────────────────────────────────────────────────────────
def test_null_sentinels_never_become_values(tmp_path, session):
    """#NULO / #NULO# / -1 / -4 all mean "not informed" — including in money columns."""
    _candidacy(tmp_path, session)
    src = _write(
        tmp_path,
        "pc_nulls.zip",
        make_prestacao_contas_zip(
            receitas=[
                pc_receita_row(
                    SQ_RECEITA="R1",
                    SQ_CANDIDATO=SQ,
                    NM_MUNICIPIO_DOADOR="#NULO#",
                    SQ_CANDIDATO_DOADOR="#NULO",
                    SG_PARTIDO_DOADOR="-1",
                    DS_CNAE_DOADOR="-4",
                    VR_RECEITA="-4",
                )
            ],
        ),
    )

    PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()

    rev = _revenue(session, "R1")
    assert rev.nm_municipio_doador is None
    assert rev.sq_candidato_doador is None
    assert rev.sg_partido_doador is None
    assert rev.ds_cnae_doador is None
    assert rev.vr_receita is None  # -4 must not land as a negative amount




def test_repeated_sq_receita_keeps_every_receipt(tmp_path, session):
    """Regression: SQ_RECEITA looks like a key but is not one.

    In the real 2022/SC file, 72 sequences span 241 extra rows, and the copies are
    different money — same candidate, turno and filing, different VR_RECEITA and
    DS_RECEITA. Keying on it dropped ~0.2% of declared revenue, so identity is a
    row hash and both rows must survive.
    """
    _candidacy(tmp_path, session)
    src = _write(
        tmp_path,
        "pc_dupe_seq.zip",
        make_prestacao_contas_zip(
            receitas=[
                pc_receita_row(
                    SQ_RECEITA="28316985", SQ_CANDIDATO=SQ,
                    VR_RECEITA="142,50", DS_RECEITA="Doação A",
                ),
                pc_receita_row(
                    SQ_RECEITA="28316985", SQ_CANDIDATO=SQ,
                    VR_RECEITA="750,00", DS_RECEITA="Doação B",
                ),
            ],
        ),
    )
    PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()

    rows = session.execute(
        select(CampaignRevenue).where(CampaignRevenue.sq_receita == "28316985")
    ).scalars().all()
    assert len(rows) == 2, "both receipts must survive a shared SQ_RECEITA"
    assert {float(r.vr_receita) for r in rows} == {142.50, 750.00}
    assert session.scalar(select(func.sum(CampaignRevenue.vr_receita))) == 892.50


def test_byte_identical_receipts_are_not_collapsed(tmp_path, session):
    """Two truly identical line items are still two receipts — and a re-run of the
    same file must not add a third."""
    _candidacy(tmp_path, session)
    rows = [
        pc_receita_row(SQ_RECEITA="R9", SQ_CANDIDATO=SQ, VR_RECEITA="100,00"),
        pc_receita_row(SQ_RECEITA="R9", SQ_CANDIDATO=SQ, VR_RECEITA="100,00"),
    ]
    src = _write(tmp_path, "pc_identical.zip", make_prestacao_contas_zip(receitas=rows))

    PrestacaoContasCollector().run(session, source=src, year=2022)
    session.commit()
    assert session.scalar(select(func.count()).select_from(CampaignRevenue)) == 2

    # Same artifact again: idempotent, not duplicated.
    src2 = _write(tmp_path, "pc_identical2.zip", make_prestacao_contas_zip(receitas=rows))
    PrestacaoContasCollector().run(session, source=src2, year=2022)
    session.commit()
    assert session.scalar(select(func.count()).select_from(CampaignRevenue)) == 2
