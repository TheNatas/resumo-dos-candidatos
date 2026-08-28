"""ALESC collectors — every upstream shape is an inline fixture, no real network.

The fixtures below are trimmed copies of live responses (verified 2026-08-18) and
deliberately keep the traps: a BOM + semicolons on the CSV, a negative refund, badge
labels that disagree with their CSS class, the `disabled` markers on session links,
and the electoral-blackout redirect target.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from resumo.db.models import AttendanceRecord, Expense, House, Mandate, Person, Proposition, Vote
from resumo.ingestion.alesc.client import AlescBlackoutError, AlescClient
from resumo.ingestion.alesc.common import MandateIndex, MandateRef, proposition_id
from resumo.ingestion.alesc.deputados import DeputadosCollector
from resumo.ingestion.alesc.despesas import DespesasCollector
from resumo.ingestion.alesc.parsing import (
    AlescParseError,
    is_electoral_blackout,
    parse_extrato_votacao,
    parse_ordem_do_dia,
    parse_presenca,
    parse_roster_payload,
    split_codigo,
)
from resumo.ingestion.alesc.presenca import PresencaCollector
from resumo.ingestion.alesc.proposicoes import ProposicoesCollector
from resumo.ingestion.alesc.votacoes import VotacoesCollector
from resumo.util import normalize_name

SITE = "https://www.alesc.sc.gov.br"
ELEGIS = "https://portalelegis.alesc.sc.gov.br"
TRANSP = "https://transparencia.alesc.sc.gov.br"
LEG = 20


# ── Fixtures (trimmed live payloads) ─────────────────────────────────────────
# UTF-8 **with BOM**, semicolon-delimited, Brazilian money, and a refund with a
# negative sign — exactly as transparência serves it.
GAB_CSV = (
    '﻿Verba;Descrição;Conta;Favorecido;Trecho;"Data de Referência";Valor\n'
    'ALMOXARIFADO;"Jornais e Revistas";"Ana Caroline Campagnolo";;;26/06/2026;1.018,80\n'
    'DIÁRIAS;"Devolução Diária Deputado";"Alex Brasil";"ALEXANDER BRASIL ALVES PEREIRA";'
    '"FLORIANÓPOLIS, SC/CHAPECÓ, SC";17/02/2026;-539,00\n'
    'TELEFONE;"Telefonia Móvel";"Deputado Que Nao Existe";"VIVO";;10/03/2026;100,00\n'
).encode()

ROSTER_CARD = """
<article class="lab-card-team mt-5"><div class="row">
  <a href="https://www.alesc.sc.gov.br/deputado/{slug}/">
    <img src="" alt="{nome}" class="lab-team-img">
    <h3 class="lab-title-news">{nome}</h3>
    <span class="lab-button px-2 py-1 lab-text mt-2" style="background: #404040;">{partido}</span>
  </a>
</div></article>
"""

SESSION_INDEX = """
<div class="d-flex justify-content-end mb-2">Exibindo 1 - 1 de 1</div>
<div class="card card-alesc mb-4"><div class="card-body">
  <h4 class="mb-1">87ª Sessão Ordinária</h4>
  <span class="text-nowrap">11/08/2026 · 14:00</span>
  <div class="d-flex">
    <a href="/sessoes-plenarias/zJo1K/ordem-do-dia" class="btn btn-sm">Ordem do Dia</a>
    <a href="/sessoes-plenarias/zJo1K/presenca" class="btn btn-sm">Presença</a>
    <a href="/sessoes-plenarias/zJo1K/ata" class="btn btn-sm disabled">Ata</a>
  </div>
</div></div>
<ul class="pagination"><li class="page-item"><a class="page-link" href="">1</a></li></ul>
"""

# One symbolic item (no extrato at all) and one nominal item (htmx trigger).
ORDEM_DO_DIA = """
<div class="border-bottom mb-3"><div class="row"><div class="col">
  <h5 class="text-success m-0 me-1"><a href="/proposicoes/KMmqv">PL./0578/2024</a></h5>
  <span class="badge text-bg-success">Aprovado</span>
  <span class="badge text-bg-light">Votação simbólica</span>
  <p class="fst-italic text-secondary mb-1">Dispõe sobre o atendimento prioritário.</p>
</div></div></div>
<div class="border-bottom mb-3"><div class="row"><div class="col">
  <h5 class="text-success m-0 me-1"><a href="/proposicoes/KgRaQ">PLC/0017/2024</a></h5>
  <span class="badge text-bg-success">Aprovado</span>
  <span class="badge text-bg-light">Votação nominal</span>
  <p class="fst-italic text-secondary mb-1">Altera a Lei Orgânica do TCE.</p>
  <p class="text-end"><a hx-get="/extrato-votacao/5Z3aR" hx-trigger="click"
     class="btn btn-sm btn-extrato-votacao">Extrato de votação</a></p>
</div></div></div>
"""

# The labels are deliberately nonsense: the position must come off the CSS class.
# The third badge uses a class ALESC has never emitted (no abstention was ever seen).
EXTRATO = """
<h4 class="row text-center">PLC/0017/2024</h4>
<h5 class="row text-center">Aguardando Discussão e Votação em 2º Turno</h5>
<div class="row">
  <div class="d-flex justify-content-between align-items-center w-100">
    <div>Alex Brasil</div><div><span class="badge text-bg-success fw-normal">SIM.</span></div>
  </div>
  <div class="d-flex justify-content-between align-items-center w-100">
    <div>Ana Campagnolo</div><div><span class="badge text-bg-danger fw-normal">NAO.</span></div>
  </div>
  <div class="d-flex justify-content-between align-items-center w-100">
    <div>Jessé Lopes</div><div><span class="badge text-bg-warning fw-normal">Abstenção</span></div>
  </div>
</div>
"""

PRESENCA = """
<table class="table table-hover">
  <tr><td>Alex Brasil</td><td>Ausência justificada</td></tr>
  <tr><td>Ana Campagnolo</td><td>Presente</td></tr>
</table>
"""

PROPOSICOES = """
<div class="d-flex justify-content-end mb-2">Exibindo 1 - 1 de 1</div>
<div class="card card-alesc mb-3"><div class="card-body">
  <h4 class="card-title"><a href="/proposicoes/574R6">PL./0534/2026</a></h4>
  <p class="mb-1 fst-italic">Declara de utilidade pública a Fundação Cultural.</p>
  <div>
    <div class="row"><div class="col-lg-2 fw-bold">Entrada</div>
      <div class="col-lg-10">03/08/2026</div></div>
    <div class="row"><div class="col-lg-2 fw-bold">Autoria</div>
      <div class="col-lg-10"><ul><li>Deputada Ana Campagnolo</li></ul></div></div>
    <div class="row"><div class="col-lg-2 fw-bold">Situação atual</div>
      <div class="col-lg-10">Aguardando apreciação pela Comissão</div></div>
  </div>
</div></div>
"""

BLACKOUT_PAGE = """
<html><head><title>Período Eleitoral - Assembleia Legislativa</title></head>
<body><h1>Período Eleitoral</h1>
<p>esta página permanecerá temporariamente indisponível</p></body></html>
"""


def _roster_payload(entries, total=None):
    html = "".join(
        ROSTER_CARD.format(slug=s, nome=n, partido=p) for s, n, p in entries
    )
    return {"html": html, "meta": {"postcount": len(entries), "totalposts": total or len(entries)}}


def _seed(session, *entries) -> dict[str, Mandate]:
    """Seed ASSEMBLEIA mandates directly (the roster collector has its own test)."""
    out = {}
    for slug, nome, partido in entries:
        person = Person(cpf=None, nome_normalizado=normalize_name(nome))
        session.add(person)
        session.flush()
        mandate = Mandate(
            house=House.ASSEMBLEIA,
            house_member_id=slug,
            id_legislatura=LEG,
            person_id=person.id,
            nome_parlamentar=nome,
            sigla_partido=partido,
            sigla_uf="SC",
        )
        session.add(mandate)
        session.flush()
        out[slug] = mandate
    return out


def _two_deputies(session):
    return _seed(session, ("ana-campagnolo", "Ana Campagnolo", "PL"),
                 ("alex-brasil", "Alex Brasil", "PL"))


# ── 1. Expenses: BOM + semicolons, and refunds keep their sign ───────────────
@respx.mock
def test_expense_csv_bom_semicolons_and_negative_refund(session):
    _two_deputies(session)
    respx.get(f"{TRANSP}/gabinetes-parlamentares/csv/2026").mock(
        return_value=httpx.Response(
            200, content=GAB_CSV, headers={"content-type": "text/csv; charset=UTF-8"}
        )
    )

    res = DespesasCollector().run(
        session, anos=[2026], datasets=["gabinetes-parlamentares"], id_legislatura=LEG
    )
    session.flush()

    assert res.status == "ingested"
    rows = session.scalars(select(Expense).order_by(Expense.house_member_id)).all()
    assert len(rows) == 2  # the third row names nobody we hold a mandate for

    refund = next(r for r in rows if r.house_member_id == "alex-brasil")
    # A "Devolução" is money given back: the sign MUST survive, or totals inflate.
    assert refund.valor_liquido == Decimal("-539.00")
    assert refund.tipo_despesa == "DIÁRIAS"
    assert refund.nome_fornecedor == "ALEXANDER BRASIL ALVES PEREIRA"
    assert (refund.ano, refund.mes) == (2026, 2)

    # The BOM was stripped: the first column parsed as "Verba", not "﻿Verba".
    ana = next(r for r in rows if r.house_member_id == "ana-campagnolo")
    assert ana.tipo_despesa == "ALMOXARIFADO"
    assert ana.valor_liquido == Decimal("1018.80")
    assert ana.house == House.ASSEMBLEIA


@respx.mock
def test_expense_collector_is_idempotent(session):
    _two_deputies(session)
    respx.get(f"{TRANSP}/gabinetes-parlamentares/csv/2026").mock(
        return_value=httpx.Response(200, content=GAB_CSV)
    )
    kwargs = dict(anos=[2026], datasets=["gabinetes-parlamentares"], id_legislatura=LEG)
    DespesasCollector().run(session, **kwargs)
    session.commit()
    second = DespesasCollector().run(session, **kwargs)
    session.commit()

    assert second.status == "skipped"  # same bytes -> ledger short-circuit
    assert session.scalar(select(Expense).where(Expense.ano == 2026)) is not None
    assert len(session.scalars(select(Expense)).all()) == 2


# ── 2. `Conta` -> mandate by normalized name; misses are logged, not silent ──
@respx.mock
def test_conta_matches_mandate_by_name_and_misses_are_logged(session, caplog):
    _two_deputies(session)
    respx.get(f"{TRANSP}/gabinetes-parlamentares/csv/2026").mock(
        return_value=httpx.Response(200, content=GAB_CSV)
    )

    with caplog.at_level(logging.WARNING, logger="resumo.ingestion.alesc"):
        res = DespesasCollector().run(
            session, anos=[2026], datasets=["gabinetes-parlamentares"], id_legislatura=LEG
        )
    session.flush()

    # "Ana Caroline Campagnolo" (civil name in the CSV) -> the "Ana Campagnolo" mandate.
    ana = session.scalar(select(Expense).where(Expense.tipo_despesa == "ALMOXARIFADO"))
    assert ana.house_member_id == "ana-campagnolo"
    assert ana.mandate_id is not None

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Deputado Que Nao Existe" in logged
    assert "unmatched name" in (res.detail or "")


def test_mandate_index_prefers_exact_and_refuses_a_weak_fuzzy_match():
    index = MandateIndex(
        [
            MandateRef("ana-campagnolo", None, "Ana Campagnolo"),
            MandateRef("sargento-lima", None, "Sargento Lima"),
            MandateRef("paulinha", None, "Paulinha"),
        ]
    )
    assert index.match("Ana Campagnolo").slug == "ana-campagnolo"
    assert index.match("Ana Caroline Campagnolo").slug == "ana-campagnolo"  # token subset
    assert index.match("Ana Paula da Silva (Paulinha)").slug == "paulinha"  # nickname variant
    # "Sargento Lima"'s civil name shares only one token — a guess here would put one
    # deputy's spending on another's public record, so it must stay unmatched.
    assert index.match("Carlos Henrique Lima") is None
    assert index.unmatched["Carlos Henrique Lima"] == 1


# ── 3. Vote positions come off the badge CSS class ──────────────────────────
def test_extrato_reads_badge_class_and_survives_an_unknown_one(caplog):
    with caplog.at_level(logging.WARNING, logger="resumo.ingestion.alesc"):
        extrato = parse_extrato_votacao(EXTRATO)

    assert extrato.codigo == "PLC/0017/2024"
    # Labels in the fixture are "SIM."/"NAO." — the class, not the text, decides.
    assert extrato.votos[0] == ("Alex Brasil", "Sim")
    assert extrato.votos[1] == ("Ana Campagnolo", "Não")
    # Unknown badge class: keep the visible label, log it, do not crash.
    assert extrato.votos[2] == ("Jessé Lopes", "Abstenção")
    assert "unknown vote badge" in "\n".join(r.getMessage() for r in caplog.records)


def test_ordem_do_dia_separates_symbolic_from_nominal():
    items = parse_ordem_do_dia(ORDEM_DO_DIA)
    assert len(items) == 2
    simbolica, nominal = items
    assert simbolica.tipo_votacao == "Votação simbólica"
    assert simbolica.extrato_hash is None
    assert simbolica.is_nominal is False
    assert nominal.is_nominal is True
    assert nominal.extrato_hash == "5Z3aR"
    assert nominal.proposicao_hash == "KgRaQ"


# ── 4. A symbolic votação produces NO Vote rows ─────────────────────────────
@respx.mock
def test_symbolic_votacao_produces_no_vote_rows(session):
    _two_deputies(session)
    respx.get(f"{ELEGIS}/sessoes-plenarias").mock(
        return_value=httpx.Response(200, text=SESSION_INDEX)
    )
    respx.get(f"{ELEGIS}/sessoes-plenarias/zJo1K/ordem-do-dia").mock(
        return_value=httpx.Response(200, text=ORDEM_DO_DIA)
    )
    extrato_route = respx.get(f"{ELEGIS}/extrato-votacao/5Z3aR").mock(
        return_value=httpx.Response(200, text=EXTRATO)
    )

    res = VotacoesCollector().run(session, id_legislatura=LEG)
    session.flush()

    votes = session.scalars(select(Vote)).all()
    # Only the two deputies we hold mandates for, and only from the NOMINAL item.
    assert {v.house_member_id for v in votes} == {"ana-campagnolo", "alex-brasil"}
    assert {v.id_votacao for v in votes} == {"AL5Z3aR"}
    # The symbolic item's proposition never appears: symbolic votings record no
    # individual position, and none may be synthesized.
    assert "ALKMmqv" not in {v.id_proposicao for v in votes}
    assert {v.id_proposicao for v in votes} == {"ALKgRaQ"}
    assert {v.tipo_voto for v in votes} == {"Sim", "Não"}
    assert all(v.orientacao_partido is None for v in votes)  # ALESC publishes none
    assert extrato_route.call_count == 1  # fetched once, for the nominal item only
    assert "1 nominal / 2 items" in (res.detail or "")


@respx.mock
def test_votacoes_with_only_symbolic_items_writes_nothing(session):
    _two_deputies(session)
    only_symbolic = ORDEM_DO_DIA.split('<div class="border-bottom mb-3">')[1]
    respx.get(f"{ELEGIS}/sessoes-plenarias").mock(
        return_value=httpx.Response(200, text=SESSION_INDEX)
    )
    respx.get(f"{ELEGIS}/sessoes-plenarias/zJo1K/ordem-do-dia").mock(
        return_value=httpx.Response(200, text=f'<div class="border-bottom mb-3">{only_symbolic}')
    )

    res = VotacoesCollector().run(session, id_legislatura=LEG)
    session.flush()

    assert session.scalars(select(Vote)).all() == []
    assert res.row_count == 0
    assert res.status == "empty"


# ── 5. Presença ─────────────────────────────────────────────────────────────
def test_presenca_parsing_marks_justified_absence():
    entries = parse_presenca(PRESENCA)
    assert entries[0].nome == "Alex Brasil"
    assert entries[0].presente is False
    assert entries[0].justificativa == "Ausência justificada"
    assert entries[1].presente is True
    assert entries[1].justificativa is None


@respx.mock
def test_presenca_collector_writes_attendance(session):
    _two_deputies(session)
    respx.get(f"{ELEGIS}/sessoes-plenarias").mock(
        return_value=httpx.Response(200, text=SESSION_INDEX)
    )
    respx.get(f"{ELEGIS}/sessoes-plenarias/zJo1K/presenca").mock(
        return_value=httpx.Response(200, text=PRESENCA)
    )

    PresencaCollector().run(session, id_legislatura=LEG)
    session.flush()

    rows = {r.house_member_id: r for r in session.scalars(select(AttendanceRecord)).all()}
    assert set(rows) == {"alex-brasil", "ana-campagnolo"}
    absent = rows["alex-brasil"]
    assert absent.presente is False
    assert absent.justificativa == "Ausência justificada"
    assert absent.derivation == "alesc_sessao_presenca"
    assert absent.id_evento == "ALzJo1K"  # prefixed: shared table with Câmara event ids
    assert str(absent.data) == "2026-08-11"
    assert rows["ana-campagnolo"].presente is True


# ── 6. Electoral blackout is detected and skipped, not raised at the caller ──
def test_blackout_banner_is_detected():
    assert is_electoral_blackout(BLACKOUT_PAGE) is True
    assert is_electoral_blackout("", "https://www.alesc.sc.gov.br/aviso-periodo-eleitoral/") is True
    assert is_electoral_blackout("<html><body>Deputada Ana Campagnolo</body></html>") is False


@respx.mock
def test_profile_page_blackout_raises_a_typed_error():
    respx.get(f"{SITE}/deputado/ana-campagnolo/").mock(
        return_value=httpx.Response(200, text=BLACKOUT_PAGE)
    )
    with AlescClient() as client, pytest.raises(AlescBlackoutError):
        client.get_site_html("/deputado/ana-campagnolo/")


@respx.mock
def test_roster_collector_reports_blackout_without_a_stack_trace(session):
    respx.get(url__startswith=f"{SITE}/wp-admin/admin-ajax.php").mock(
        return_value=httpx.Response(200, text=BLACKOUT_PAGE)
    )

    res = DeputadosCollector().run(session, id_legislatura=LEG)

    assert res.status == "error"
    assert "admin-ajax" in (res.detail or "")
    assert session.scalars(select(Mandate)).all() == []


# ── 7. Roster: happy path, and clear errors on upstream drift ───────────────
@respx.mock
def test_roster_collector_seeds_mandate_and_cpf_less_person(session):
    respx.get(url__startswith=f"{SITE}/wp-admin/admin-ajax.php").mock(
        return_value=httpx.Response(
            200,
            json=_roster_payload(
                [("ze-caramori", "Zé Caramori", "PSD"), ("paulinha", "Paulinha", "Podemos")]
            ),
        )
    )

    res = DeputadosCollector().run(session, id_legislatura=LEG)
    session.flush()

    assert res.row_count == 2
    mandates = {m.house_member_id: m for m in session.scalars(select(Mandate)).all()}
    assert set(mandates) == {"ze-caramori", "paulinha"}
    ze = mandates["ze-caramori"]
    assert ze.house == House.ASSEMBLEIA
    assert ze.sigla_uf == "SC"  # the state lives on the mandate; the enum stays national
    assert ze.sigla_partido == "PSD"
    assert ze.nome_parlamentar == "Zé Caramori"
    # 🚨 ALESC publishes no CPF and no birth date: identity is name-only.
    person = session.get(Person, ze.person_id)
    assert person.cpf is None
    assert person.data_nascimento is None
    assert person.nome_normalizado == "ZE CARAMORI"


@respx.mock
def test_roster_collector_is_idempotent(session):
    respx.get(url__startswith=f"{SITE}/wp-admin/admin-ajax.php").mock(
        return_value=httpx.Response(200, json=_roster_payload([("paulinha", "Paulinha", "Podemos")]))
    )
    DeputadosCollector().run(session, id_legislatura=LEG)
    session.commit()
    DeputadosCollector().run(session, id_legislatura=LEG)
    session.commit()

    assert len(session.scalars(select(Mandate)).all()) == 1
    assert len(session.scalars(select(Person)).all()) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],                              # a bare list instead of the envelope
        {"data": [], "success": True},   # the plugin renamed its keys
        {"html": "", "meta": {}},        # empty render
        {"html": None, "meta": None},    # nulls where strings were promised
    ],
)
def test_roster_payload_drift_raises_a_clear_parse_error(payload):
    with pytest.raises(AlescParseError) as exc:
        parse_roster_payload(payload)
    assert "admin-ajax" in str(exc.value)


@respx.mock
def test_roster_collector_turns_payload_drift_into_a_result(session):
    respx.get(url__startswith=f"{SITE}/wp-admin/admin-ajax.php").mock(
        return_value=httpx.Response(200, json={"data": [], "success": True})
    )

    res = DeputadosCollector().run(session, id_legislatura=LEG)

    assert res.status == "error"
    assert res.row_count == 0
    assert "alm_get_posts contract changed" in (res.detail or "")
    assert "fallback=True" in (res.detail or "")


@respx.mock
def test_roster_collector_reports_an_unrecognized_card_layout(session):
    respx.get(url__startswith=f"{SITE}/wp-admin/admin-ajax.php").mock(
        return_value=httpx.Response(
            200, json={"html": "<section>redesigned</section>", "meta": {"totalposts": 61}}
        )
    )

    res = DeputadosCollector().run(session, id_legislatura=LEG)

    assert res.status == "empty"
    assert "roster markup changed" in (res.detail or "")


# ── Proposições ─────────────────────────────────────────────────────────────
def test_split_codigo_and_id_prefix():
    assert split_codigo("PL./0534/2026") == ("PL.", 534, 2026)
    assert split_codigo("RQS/2862/2026") == ("RQS", 2862, 2026)
    assert split_codigo("weird") == ("weird", None, None)
    # e-Legis hashids are opaque; the AL prefix keeps them out of Câmara's id space.
    assert proposition_id("574R6") == "AL574R6"


@respx.mock
def test_proposicoes_collector_writes_prefixed_ids(session):
    _seed(session, ("ana-campagnolo", "Ana Campagnolo", "PL"))
    respx.get(url__startswith=f"{ELEGIS}/proposicoes/processo-legislativo").mock(
        return_value=httpx.Response(200, text=PROPOSICOES)
    )

    ProposicoesCollector().run(session, anos=[2026], id_legislatura=LEG)
    session.flush()

    prop = session.scalar(select(Proposition))
    assert prop.proposition_id == "AL574R6"
    assert prop.house == House.ASSEMBLEIA
    assert (prop.sigla_tipo, prop.numero, prop.ano) == ("PL.", 534, 2026)
    assert prop.situacao == "Aguardando apreciação pela Comissão"
    assert str(prop.data_apresentacao) == "2026-08-03"
    assert prop.authoring_mandate_id is not None


def test_max_pages_none_means_unbounded(respx_mock):
    """`None` is "no cap" everywhere else in the collectors (`limit`, `anos`), and the
    CLI passes it straight through. `range(None)` raised TypeError, so a plain
    `--inicio/--fim` crawl died before fetching anything."""
    import inspect

    from resumo.ingestion.alesc import sessoes

    sig = inspect.signature(sessoes.iter_sessions)
    assert sig.parameters["max_pages"].annotation == "int | None"

    # The index yields one page with no rel=next, so an unbounded crawl still stops.
    respx_mock.get(url__regex=r".*/sessoes-plenarias.*").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )
    with AlescClient() as client:
        assert list(sessoes.iter_sessions(client, max_pages=None)) == []


# ── Atribuição de despesa pelo nome civil que o TSE publica ──────────────────
def test_mandate_index_learns_the_civil_name_from_the_tse(session):
    """A ALESC fala de si com dois vocabulários: o e-Legis usa o nome parlamentar
    ("Sargento Lima") e o portal da transparência o nome civil ("CARLOS HENRIQUE DE
    LIMA"). Sem costurar os dois, o gasto fica sem dono — e atribuí-lo por
    semelhança poria o gasto de um deputado na ficha de outro."""
    from resumo.db.models import Candidacy, House, Mandate
    from resumo.ingestion.alesc.common import mandate_index

    mandate = Mandate(
        house=House.ASSEMBLEIA, house_member_id="sargento-lima", id_legislatura=20,
        sigla_uf="SC", nome_parlamentar="Sargento Lima",
    )
    session.add(mandate)
    session.add(
        Candidacy(
            sq_candidato="P22", ano_eleicao=2022, sg_uf="SC", cd_cargo=7,
            ds_cargo="DEPUTADO ESTADUAL", nome_candidato="CARLOS HENRIQUE DE LIMA",
            nome_urna="Sargento Lima", nome_normalizado="CARLOS HENRIQUE DE LIMA",
            cpf_raw="12345678909", ds_sit_tot_turno="ELEITO POR QP",
        )
    )
    session.commit()

    index = mandate_index(session, 20)

    hit = index.match("CARLOS HENRIQUE DE LIMA")
    assert hit is not None and hit.mandate_id == mandate.id
    # E o nome parlamentar continua resolvendo, claro.
    assert index.match("Sargento Lima").mandate_id == mandate.id


def test_mandate_index_still_works_without_a_bridge(session):
    """Sem candidatura anterior correspondente não há alias, e o índice volta a ser
    exatamente o de antes — a ponte só acrescenta, nunca substitui."""
    from resumo.db.models import House, Mandate
    from resumo.ingestion.alesc.common import mandate_index

    session.add(
        Mandate(
            house=House.ASSEMBLEIA, house_member_id="fulano", id_legislatura=20,
            sigla_uf="SC", nome_parlamentar="Fulano de Tal",
        )
    )
    session.commit()

    index = mandate_index(session, 20)

    assert index.refs[0].aliases == ()
    assert index.match("Fulano de Tal") is not None
    assert index.match("NOME CIVIL DESCONHECIDO") is None


# ── iniciativa: o filtro que a fonte ignora em silêncio ──────────────────────
INICIATIVA_SELECT = """
<select name="iniciativa">
  <option value="">Todos</option>
  <option value="ana-campagnolo">Deputada Ana Campagnolo</option>
  <option value="vanessa-da-rosa">Deputada Prof. Vanessa da Rosa</option>
  <option value="pedro-baldissera">Deputado Padre Pedro Baldissera</option>
  <option value="camilo-martins">Deputado Camilo Martins</option>
  <option value="nazareno-martins">Nazareno Martins</option>
</select>
"""


def _house_wide(n: int) -> str:
    """A listagem sem filtro: página cheia e um total grande, como a Casa inteira."""
    cards = "".join(
        f'<div class="card card-alesc mb-3"><div class="card-body">'
        f'<h4 class="card-title"><a href="/proposicoes/h{i}">PL./{i:04d}/2026</a></h4>'
        f'<p class="mb-1 fst-italic">Ementa {i}.</p></div></div>'
        for i in range(10)
    )
    return (
        f'{INICIATIVA_SELECT}'
        f'<div class="d-flex justify-content-end mb-2">Exibindo 1 - 10 de {n}</div>{cards}'
    )


def test_iniciativa_resolves_the_elegis_vocabulary_not_the_profile_slug(session):
    """O slug do perfil e o valor de `iniciativa` são vocabulários diferentes."""
    from resumo.ingestion.alesc.common import mandate_index
    from resumo.ingestion.alesc.proposicoes import resolve_iniciativa

    _seed(
        session,
        ("ana-campagnolo", "Ana Campagnolo", "PL"),
        ("profa-vanessa-da-rosa", "Profª Vanessa da Rosa", "PP"),
        ("padre-pedro-baldissera", "Padre Pedro Baldissera", "PT"),
        ("camilo-martins", "Camilo Martins", "PL"),
        ("delegado-egidio", "Delegado Egidio", "PL"),
    )

    class _Stub:
        def get_elegis(self, path, params=None, **kw):
            return _house_wide(531)

    resolved, unresolved = resolve_iniciativa(
        _Stub(), mandate_index(session, LEG), "processo-legislativo"
    )
    # Igualdade de string quando a fonte publica o próprio slug.
    assert resolved["ana-campagnolo"] == "ana-campagnolo"
    # Por nome, quando os dois vocabulários divergem — este é o caso que corrompia.
    assert resolved["profa-vanessa-da-rosa"] == "vanessa-da-rosa"
    assert resolved["padre-pedro-baldissera"] == "pedro-baldissera"
    # "Camilo Martins" também casa fuzzy com "Nazareno Martins"; a igualdade exata
    # do passo 1 decide, em vez de a ambiguidade descartar o deputado.
    assert resolved["camilo-martins"] == "camilo-martins"
    # Sem opção alguma: não é coletado, e é reportado.
    assert unresolved == ["delegado-egidio"]
    assert "delegado-egidio" not in resolved


@respx.mock
def test_unrecognised_iniciativa_never_attributes_the_whole_house(session):
    """🚨 e-Legis devolve a Casa inteira quando não reconhece o `iniciativa`.

    Sem defesa, as 531 proposições da Casa viram autoria de um deputado só.
    """
    _seed(session, ("delegado-egidio", "Delegado Egidio", "PL"))
    respx.get(url__startswith=f"{ELEGIS}/proposicoes/processo-legislativo").mock(
        return_value=httpx.Response(200, text=_house_wide(531))
    )

    result = ProposicoesCollector().run(session, anos=[2026], id_legislatura=LEG)
    session.flush()

    assert session.scalars(select(Proposition)).all() == []
    assert "sem `iniciativa`" in result.detail


@respx.mock
def test_sentinel_refuses_a_response_identical_to_the_unfiltered_one(session):
    """Segunda linha de defesa: um valor aceito na validação que o servidor ignora
    assim mesmo. A resposta é reconhecida pelo total anunciado."""
    _seed(session, ("ana-campagnolo", "Ana Campagnolo", "PL"))
    # `ana-campagnolo` É uma opção publicada, então passa por `resolve_iniciativa`;
    # o servidor responde a Casa inteira de qualquer forma.
    respx.get(url__startswith=f"{ELEGIS}/proposicoes/processo-legislativo").mock(
        return_value=httpx.Response(200, text=_house_wide(531))
    )

    ProposicoesCollector().run(session, anos=[2026], id_legislatura=LEG)
    session.flush()
    assert session.scalars(select(Proposition)).all() == []


@respx.mock
def test_sentinel_does_not_fire_on_a_genuinely_small_result(session):
    """Um deputado cujo total coincide com o da Casa num ano magro é coincidência
    plausível, não filtro ignorado — o piso existe para não descartar dado real."""
    _seed(session, ("ana-campagnolo", "Ana Campagnolo", "PL"))
    respx.get(url__startswith=f"{ELEGIS}/proposicoes/processo-legislativo").mock(
        return_value=httpx.Response(200, text=INICIATIVA_SELECT + PROPOSICOES)
    )

    ProposicoesCollector().run(session, anos=[2026], id_legislatura=LEG)
    session.flush()
    prop = session.scalar(select(Proposition))
    assert prop is not None and prop.proposition_id == "AL574R6"
