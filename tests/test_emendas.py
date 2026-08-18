"""Emendas parlamentares: bulk ingestion, type mapping and the author bridge.

Fixtures are synthetic but structurally faithful to the real CGU file (latin-1,
';'-delimited, every field quoted, decimal comma, full state NAME in `UF`, the
three-member zip). No network.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile

import pytest
from sqlalchemy import func, select

from resumo.db.models import (
    AmendmentAuthorLink,
    AmendmentType,
    BudgetAmendment,
    ConfidenceTier,
    House,
    Mandate,
    MatchMethod,
    Person,
)
from resumo.ingestion.emendas import author_bridge, parsing
from resumo.ingestion.emendas.emendas_parlamentares import EmendasParlamentaresCollector
from resumo.util import normalize_name

# The published header, verbatim (note "Número da emenda" and "Código Programa").
EMENDAS_COLUMNS = [
    "Código da Emenda", "Ano da Emenda", "Tipo de Emenda", "Código do Autor da Emenda",
    "Nome do Autor da Emenda", "Número da emenda", "Localidade de aplicação do recurso",
    "Código Município IBGE", "Município", "Código UF IBGE", "UF", "Região",
    "Código Função", "Nome Função", "Código Subfunção", "Nome Subfunção",
    "Código Programa", "Nome Programa", "Código Ação", "Nome Ação",
    "Código Plano Orçamentário", "Nome Plano Orçamentário", "Valor Empenhado",
    "Valor Liquidado", "Valor Pago", "Valor Restos A Pagar Inscritos",
    "Valor Restos A Pagar Cancelados", "Valor Restos A Pagar Pagos",
]

TIPO_FINALIDADE = "Emenda Individual - Transferências com Finalidade Definida"
TIPO_ESPECIAL = "Emenda Individual - Transferências Especiais"
TIPO_BANCADA = "Emenda de Bancada"
TIPO_COMISSAO = "Emenda de Comissão"
TIPO_RELATOR = "Emenda de Relator"


def emenda_row(**over) -> dict[str, str]:
    """One SC individual-amendment row, with the source's real shape."""
    base = {
        "Código da Emenda": "202543010001",
        "Ano da Emenda": "2025",
        "Tipo de Emenda": TIPO_FINALIDADE,
        "Código do Autor da Emenda": "4301",
        "Nome do Autor da Emenda": "ANA PAULA LIMA",
        "Número da emenda": "0001",
        "Localidade de aplicação do recurso": "JOINVILLE - SC",
        "Código Município IBGE": "4209102",
        "Município": "JOINVILLE",
        "Código UF IBGE": "4200000",
        "UF": "SANTA CATARINA",  # 🚨 full state NAME, never "SC"
        "Região": "Sul",
        "Código Função": "10",
        "Nome Função": "Saúde",
        "Código Subfunção": "301",
        "Nome Subfunção": "Atenção básica",
        "Código Programa": "2015",
        "Nome Programa": "FORTALECIMENTO DO SISTEMA UNICO DE SAUDE (SUS)",
        "Código Ação": "8581",
        "Nome Ação": "ESTRUTURACAO DA REDE DE SERVICOS DE ATENCAO PRIMARIA A SAUDE",
        "Código Plano Orçamentário": "0000",
        "Nome Plano Orçamentário": "EMENDA INDIVIDUAL",
        "Valor Empenhado": "899920,98",
        "Valor Liquidado": "0,00",
        "Valor Pago": "0,00",
        "Valor Restos A Pagar Inscritos": "0,00",
        "Valor Restos A Pagar Cancelados": "0,00",
        "Valor Restos A Pagar Pagos": "899920,98",
    }
    base.update({k: str(v) for k, v in over.items()})
    return base


def _csv_bytes(columns: list[str], rows: list[dict]) -> bytes:
    text = io.StringIO()
    writer = csv.DictWriter(
        text, fieldnames=columns, delimiter=";", quoting=csv.QUOTE_ALL, extrasaction="ignore"
    )
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return text.getvalue().encode("latin-1")


def make_emendas_zip(rows: list[dict], *, columns: list[str] | None = None) -> bytes:
    """The three-member zip. The siblings are filled with junk on purpose: reading
    them as the main table must be impossible, not merely unlikely."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("EmendasParlamentares.csv", _csv_bytes(columns or EMENDAS_COLUMNS, rows))
        zf.writestr("EmendasParlamentares_Convenios.csv", b'"Codigo da Emenda";"Convenio"\r\n')
        zf.writestr("EmendasParlamentares_PorFavorecido.csv", b'"Codigo da Emenda";"Favorecido"\r\n')
    return buf.getvalue()


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _mandate(
    session,
    nome: str,
    uf: str,
    *,
    member_id: str,
    house: House = House.CAMARA,
    leg: int = 57,
) -> Mandate:
    person = Person(nome_civil=nome, nome_normalizado=normalize_name(nome))
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=house,
        house_member_id=member_id,
        id_legislatura=leg,
        person_id=person.id,
        nome_parlamentar=nome,
        sigla_uf=uf,
    )
    session.add(mandate)
    session.flush()
    return mandate


# ── UF filter ────────────────────────────────────────────────────────────────
def test_uf_filter_matches_the_full_state_name_not_the_sigla(tmp_path, session):
    rows = [
        emenda_row(**{"Código da Emenda": "202543010001"}),  # SANTA CATARINA
        emenda_row(
            **{
                "Código da Emenda": "202512340001",
                "UF": "PARANÁ",
                "Código UF IBGE": "4100000",
                "Município": "IVAIPORÃ",
                "Código Município IBGE": "4111506",
            }
        ),
        # A row whose UF cell holds the sigla: the source does NOT publish this, so
        # the name-based filter must not match it (it is only flagged, via the code).
        emenda_row(**{"Código da Emenda": "202543010002", "UF": "SC"}),
    ]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip(rows))

    res = EmendasParlamentaresCollector().run(session, source=src, ufs=["SC"])
    session.commit()

    assert res.status == "ingested"
    assert res.row_count == 1
    (ingested,) = session.scalars(select(BudgetAmendment)).all()
    assert ingested.uf == "SANTA CATARINA"
    assert ingested.codigo_emenda == "202543010001"
    # The IBGE code corroborates the dropped row, so the collector says so loudly
    # instead of silently reporting an empty ingestion.
    assert "IBGE code only" in (res.detail or "")


def test_uf_scope_translates_siglas_and_rejects_nonsense():
    scope = parsing.uf_scope(["sc"])
    assert scope.siglas == ("SC",)
    assert "SANTA CATARINA" in scope.names
    assert "4200000" in scope.codes
    assert parsing.uf_scope([]).is_national is True
    with pytest.raises(parsing.UnknownUfError):
        parsing.uf_scope(["XX"])


def test_national_run_keeps_every_state(tmp_path, session):
    rows = [
        emenda_row(**{"Código da Emenda": "202543010001"}),
        emenda_row(**{"Código da Emenda": "202512340001", "UF": "PARANÁ", "Código UF IBGE": "4100000"}),
    ]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip(rows))

    res = EmendasParlamentaresCollector().run(session, source=src, ufs=[])
    session.commit()

    assert res.row_count == 2


# ── Tipo de emenda ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected,individual",
    [
        (TIPO_FINALIDADE, AmendmentType.individual_finalidade_definida, True),
        (TIPO_ESPECIAL, AmendmentType.individual_transferencia_especial, True),
        (TIPO_BANCADA, AmendmentType.bancada, False),
        (TIPO_COMISSAO, AmendmentType.comissao, False),
        (TIPO_RELATOR, AmendmentType.relator, False),
        ("emenda individual - transferências especiais", AmendmentType.individual_transferencia_especial, True),
        ("Emenda de Relator-Geral", AmendmentType.relator, False),
        ("Emenda de Coisa Nova", AmendmentType.outro, False),
        ("", AmendmentType.outro, False),
    ],
)
def test_tipo_mapping(raw, expected, individual):
    tipo = parsing.map_tipo(raw)
    assert tipo is expected
    assert tipo.is_individual is individual


def test_only_the_two_individual_types_are_individual():
    individuals = {t for t in AmendmentType if t.is_individual}
    assert individuals == {
        AmendmentType.individual_finalidade_definida,
        AmendmentType.individual_transferencia_especial,
    }


# ── SIOP author code ─────────────────────────────────────────────────────────
def test_author_code_extracted_from_emenda_code():
    assert parsing.author_code_from_emenda("202543010001") == "4301"
    assert parsing.author_code_from_emenda("202028500042") == "2850"
    assert parsing.author_code_from_emenda("Sem informação") is None
    assert parsing.author_code_from_emenda("20254301000") is None  # 11 digits


def test_author_code_falls_back_to_the_emenda_code(tmp_path, session):
    rows = [emenda_row(**{"Código do Autor da Emenda": "S/I"})]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip(rows))

    EmendasParlamentaresCollector().run(session, source=src, ufs=["SC"])
    session.commit()

    (row,) = session.scalars(select(BudgetAmendment)).all()
    assert row.siop_author_code == "4301"


def test_author_code_namespace_is_partitioned():
    assert parsing.author_code_kind("4301") == "individual"
    assert parsing.author_code_kind("9055") == "individual"
    assert parsing.author_code_kind("5021") == "comissao"
    assert parsing.author_code_kind("6006") == "comissao"  # Senado committees
    assert parsing.author_code_kind("7126") == "bancada"
    assert parsing.author_code_kind("8100") == "relator"
    assert parsing.author_code_kind("-99") == "desconhecido"  # relator-geral sentinel
    assert parsing.is_structurally_individual("7126") is False


# ── Sentinels / municipality ─────────────────────────────────────────────────
def test_multiplo_is_not_stored_as_a_municipality(tmp_path, session):
    rows = [
        emenda_row(
            **{
                "Código da Emenda": "202543010007",
                "Localidade de aplicação do recurso": "MÚLTIPLO",
                "Código Município IBGE": "Sem informação",
                "Município": "Múltiplo",
            }
        ),
        emenda_row(
            **{
                "Código da Emenda": "202543010008",
                "Localidade de aplicação do recurso": "SANTA CATARINA (UF)",
                "Código Município IBGE": "Sem informação",
                "Município": "Sem informação",
            }
        ),
    ]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip(rows))

    EmendasParlamentaresCollector().run(session, source=src, ufs=["SC"])
    session.commit()

    stored = session.scalars(select(BudgetAmendment)).all()
    assert len(stored) == 2
    assert all(r.municipio is None and r.codigo_municipio_ibge is None for r in stored)
    assert all(r.uf == "SANTA CATARINA" for r in stored)


def test_rows_without_a_codigo_da_emenda_are_dropped_and_counted(tmp_path, session):
    rows = [
        emenda_row(),
        emenda_row(
            **{
                "Código da Emenda": "Sem informação",
                "Código do Autor da Emenda": "S/I",
                "Nome do Autor da Emenda": "Sem informação",
                "Código Ação": "4525",
            }
        ),
    ]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip(rows))

    res = EmendasParlamentaresCollector().run(session, source=src, ufs=["SC"])
    session.commit()

    assert res.row_count == 1
    assert "without código da emenda" in (res.detail or "")


def test_missing_column_fails_loudly(tmp_path):
    columns = [c for c in EMENDAS_COLUMNS if c != "Valor Empenhado"]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip([emenda_row()], columns=columns))

    with pytest.raises(parsing.MissingColumnsError) as exc:
        list(parsing.iter_records(src))
    assert "Valor Empenhado" in str(exc.value)


def test_decimal_comma_and_latin1_survive(tmp_path, session):
    rows = [emenda_row(**{"Município": "CRICIÚMA", "Valor Empenhado": "1.200.000,50"})]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip(rows))

    EmendasParlamentaresCollector().run(session, source=src, ufs=["SC"])
    session.commit()

    (row,) = session.scalars(select(BudgetAmendment)).all()
    assert float(row.valor_empenhado) == 1200000.50
    assert row.municipio == "CRICIÚMA"


# ── Idempotency ──────────────────────────────────────────────────────────────
def test_reingestion_is_idempotent(tmp_path, session):
    rows = [emenda_row(), emenda_row(**{"Código da Emenda": "202543010002", "Código Ação": "4525"})]
    src = _write(tmp_path, "emendas.zip", make_emendas_zip(rows))
    collector = EmendasParlamentaresCollector()

    first = collector.run(session, source=src, ufs=["SC"])
    session.commit()
    second = collector.run(session, source=src, ufs=["SC"])
    session.commit()

    assert first.status == "ingested" and first.row_count == 2
    assert second.status == "skipped"  # same bytes -> ledger no-op
    assert session.scalar(select(func.count()).select_from(BudgetAmendment)) == 2

    # A monthly refresh moves the money but not the grain: the same rows must be
    # UPDATED, never duplicated.
    refreshed = [
        emenda_row(**{"Valor Empenhado": "999999,99"}),
        emenda_row(**{"Código da Emenda": "202543010002", "Código Ação": "4525"}),
    ]
    third = collector.run(
        session, source=_write(tmp_path, "emendas2.zip", make_emendas_zip(refreshed)), ufs=["SC"]
    )
    session.commit()

    assert third.status == "ingested"
    assert session.scalar(select(func.count()).select_from(BudgetAmendment)) == 2
    updated = session.scalar(
        select(BudgetAmendment).where(BudgetAmendment.codigo_emenda == "202543010001")
    )
    assert float(updated.valor_empenhado) == 999999.99


# ── Author bridge ────────────────────────────────────────────────────────────
def _ingest(session, tmp_path, rows, name="emendas.zip", ufs=("SC",)):
    src = _write(tmp_path, name, make_emendas_zip(rows))
    res = EmendasParlamentaresCollector().run(session, source=src, ufs=list(ufs))
    session.commit()
    return res


def test_uf_scoped_exact_name_match_links_the_author(tmp_path, session):
    mandate = _mandate(session, "Ana Paula Lima", "SC", member_id="204321")
    session.commit()
    _ingest(session, tmp_path, [emenda_row()])

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.linked == 1 and result.unlinked == 0
    link = session.scalar(select(AmendmentAuthorLink))
    assert (link.siop_author_code, link.ano) == ("4301", 2025)
    assert link.mandate_id == mandate.id
    assert link.match_method == MatchMethod.probabilistic
    assert link.confidence_tier == ConfidenceTier.auto_strong
    assert link.resolver == author_bridge.RESOLVER_VERSION
    # ...and the amendment rows carry the resolved author.
    row = session.scalar(select(BudgetAmendment))
    assert row.mandate_id == mandate.id and row.person_id == mandate.person_id
    assert result.amendments_linked == 1


def test_same_name_in_another_uf_is_never_linked(tmp_path, session):
    # The false-positive guard: an exact name match outside the emenda's UF must not
    # produce an edge (unscoped, this data really does match e.g. Rodrigo Pacheco to
    # RODRIGO COELHO).
    _mandate(session, "Ana Paula Lima", "MG", member_id="777777")
    session.commit()
    _ingest(session, tmp_path, [emenda_row()])

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.linked == 0 and result.unlinked == 1
    assert session.scalar(select(func.count()).select_from(AmendmentAuthorLink)) == 0
    assert session.scalar(select(BudgetAmendment)).mandate_id is None
    assert "no mandate named" in result.unresolved[0][3]


def test_uf_scope_picks_the_right_homonym(tmp_path, session):
    sc = _mandate(session, "Ana Paula Lima", "SC", member_id="204321")
    _mandate(session, "Ana Paula Lima", "MG", member_id="777777")
    session.commit()
    _ingest(session, tmp_path, [emenda_row()])

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.linked == 1
    assert session.scalar(select(AmendmentAuthorLink)).mandate_id == sc.id


def test_ambiguous_author_is_left_unlinked(tmp_path, session):
    # Two mandates, same name, same UF, both covering 2025: nothing here may guess.
    _mandate(session, "Ana Paula Lima", "SC", member_id="204321")
    _mandate(session, "Ana Paula Lima", "SC", member_id="204322")
    session.commit()
    _ingest(session, tmp_path, [emenda_row()])

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.linked == 0 and result.unlinked == 1
    assert session.scalar(select(func.count()).select_from(AmendmentAuthorLink)) == 0
    assert session.scalar(select(BudgetAmendment)).mandate_id is None
    assert "ambiguous" in result.unresolved[0][3]


def test_bancada_and_comissao_are_never_linked_to_a_person(tmp_path, session):
    # A bancada emenda belongs to the whole state delegation and a comissão emenda to
    # a committee — even though a same-named mandate exists, no edge may be created.
    _mandate(session, "Bancada de Santa Catarina", "SC", member_id="900001")
    _mandate(session, "Comissao de Seguridade Social e Familia - CSSF", "SC", member_id="900002")
    session.commit()
    rows = [
        emenda_row(
            **{
                "Código da Emenda": "202571260001",
                "Tipo de Emenda": TIPO_BANCADA,
                "Código do Autor da Emenda": "7126",
                "Nome do Autor da Emenda": "BANCADA DE SANTA CATARINA",
            }
        ),
        emenda_row(
            **{
                "Código da Emenda": "202550210001",
                "Tipo de Emenda": TIPO_COMISSAO,
                "Código do Autor da Emenda": "5021",
                "Nome do Autor da Emenda": "COMISSAO DE SEGURIDADE SOCIAL E FAMILIA - CSSF",
            }
        ),
        emenda_row(
            **{
                "Código da Emenda": "202581000001",
                "Tipo de Emenda": TIPO_RELATOR,
                "Código do Autor da Emenda": "8100",
                "Nome do Autor da Emenda": "RELATOR GERAL",
            }
        ),
    ]
    _ingest(session, tmp_path, rows)

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.codes == 0 and result.linked == 0
    assert session.scalar(select(func.count()).select_from(AmendmentAuthorLink)) == 0
    stored = session.scalars(select(BudgetAmendment)).all()
    assert {r.tipo for r in stored} == {
        AmendmentType.bancada, AmendmentType.comissao, AmendmentType.relator
    }
    assert all(r.mandate_id is None and r.person_id is None for r in stored)


def test_succession_annotation_is_stripped_for_matching(tmp_path, session):
    # 2925 is CARMEN ZANOTTO up to 2024, then her successor's name is published with
    # the reassignment spelled out in parentheses. The holder in front of it is who
    # the source attributes execution to; the full string stays auditable.
    geovania = _mandate(session, "Geovania de Sá", "SC", member_id="204350")
    session.commit()
    raw = (
        "GEOVANIA DE SA (EX-PARLAMENTAR CARMEN ZANOTTO, NOS TERMOS ART. 78 LDO 2025 "
        "E DA MENSAGEM 95-CN, DE 06.11.25)"
    )
    assert author_bridge.display_name(raw) == "GEOVANIA DE SA"
    _ingest(
        session,
        tmp_path,
        [
            emenda_row(
                **{
                    "Código da Emenda": "202529250001",
                    "Código do Autor da Emenda": "2925",
                    "Nome do Autor da Emenda": raw,
                }
            )
        ],
    )

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.linked == 1
    link = session.scalar(select(AmendmentAuthorLink))
    assert link.mandate_id == geovania.id
    assert link.author_name_raw == raw  # reassignment preserved verbatim


def test_the_same_person_may_hold_two_codes_in_one_year(tmp_path, session):
    geovania = _mandate(session, "Geovania de Sá", "SC", member_id="204350")
    session.commit()
    rows = [
        emenda_row(
            **{
                "Código da Emenda": "202529250001",
                "Código do Autor da Emenda": "2925",
                "Nome do Autor da Emenda": "GEOVANIA DE SA (EX-PARLAMENTAR CARMEN ZANOTTO)",
            }
        ),
        emenda_row(
            **{
                "Código da Emenda": "202532350001",
                "Código do Autor da Emenda": "3235",
                "Nome do Autor da Emenda": "GEOVANIA DE SA",
            }
        ),
    ]
    _ingest(session, tmp_path, rows)

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.linked == 2
    links = session.scalars(select(AmendmentAuthorLink)).all()
    assert {link.siop_author_code for link in links} == {"2925", "3235"}
    assert {link.mandate_id for link in links} == {geovania.id}


def test_author_bridge_is_idempotent_and_keeps_manual_decisions(tmp_path, session):
    mandate = _mandate(session, "Ana Paula Lima", "SC", member_id="204321")
    other = _mandate(session, "Outro Deputado", "SC", member_id="204399")
    session.commit()
    _ingest(session, tmp_path, [emenda_row()])

    author_bridge.resolve_authors(session)
    session.commit()

    # A human overrides the machine's edge...
    link = session.scalar(select(AmendmentAuthorLink))
    link.mandate_id = other.id
    link.person_id = other.person_id
    link.match_method = MatchMethod.manual
    session.commit()

    # ...and re-running must not clobber it.
    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.manual_kept == 1
    assert session.scalar(select(func.count()).select_from(AmendmentAuthorLink)) == 1
    kept = session.scalar(select(AmendmentAuthorLink))
    assert kept.mandate_id == other.id
    assert kept.match_method == MatchMethod.manual
    assert session.scalar(select(BudgetAmendment)).mandate_id == other.id
    assert mandate.id != other.id


def test_senate_and_chamber_terms_of_the_same_name_are_split_by_year(tmp_path, session):
    # Esperidião Amin is 2850 as a deputy (leg 55, 2015-2019) and 2210 as a senator.
    # Same name, same UF, two mandates: the budget year is what disambiguates.
    deputado = _mandate(session, "Esperidião Amin", "SC", member_id="74158", leg=55)
    senador = _mandate(session, "Esperidião Amin", "SC", member_id="4981", house=House.SENADO)
    senador.data_inicio = dt.date(2019, 2, 1)
    session.commit()
    rows = [
        emenda_row(
            **{
                "Código da Emenda": "201628500001",
                "Ano da Emenda": "2016",
                "Código do Autor da Emenda": "2850",
                "Nome do Autor da Emenda": "ESPERIDIAO AMIN",
            }
        ),
        emenda_row(
            **{
                "Código da Emenda": "202522100001",
                "Ano da Emenda": "2025",
                "Código do Autor da Emenda": "2210",
                "Nome do Autor da Emenda": "ESPERIDIAO AMIN",
            }
        ),
    ]
    _ingest(session, tmp_path, rows)

    result = author_bridge.resolve_authors(session)
    session.commit()

    assert result.linked == 2
    by_code = {
        link.siop_author_code: link for link in session.scalars(select(AmendmentAuthorLink)).all()
    }
    assert by_code["2850"].ano == 2016 and by_code["2850"].mandate_id == deputado.id
    assert by_code["2210"].ano == 2025 and by_code["2210"].mandate_id == senador.id
