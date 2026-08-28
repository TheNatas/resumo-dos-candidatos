"""Executive-branch collectors, and the incumbency claim they exist to support.

The bug these were written against: a sitting governor seeking a second term showed
no "tentando reeleição" badge. Nothing was broken in the matcher — Jorginho Mello
resolved perfectly to his *old* Senate term (2019-2022, `data_fim` set, therefore not
active), and no source in the platform knew a governorship exists. So the tests here
are mostly about the two claims the ficha makes and how they can go wrong:

* a mandate is only "current" while `data_fim IS NULL`, and
* only the SAME office is re-election.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select

from resumo import cargos
from resumo.api import queries
from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    ConfidenceTier,
    House,
    Mandate,
    Person,
    Proposition,
)
from resumo.ingestion.executivo.atos import AtosCollector
from resumo.ingestion.executivo.governadores import GovernadoresCollector
from resumo.resolution.pipeline import resolve

ELEGIS = "https://portalelegis.alesc.sc.gov.br"
CPF = "25084119904"


def _candidacy(session, sq, *, ano, cargo=cargos.GOVERNADOR, sit="ELEITO", uf="SC", **over):
    row = Candidacy(
        sq_candidato=sq,
        ano_eleicao=ano,
        nr_turno=over.pop("nr_turno", 1),
        cpf_raw=over.pop("cpf_raw", CPF),
        nome_candidato=over.pop("nome_candidato", "JORGINHO DOS SANTOS MELLO"),
        nome_urna=over.pop("nome_urna", "JORGINHO MELLO"),
        nome_normalizado=over.pop("nome_normalizado", "JORGINHO DOS SANTOS MELLO"),
        data_nascimento=over.pop("data_nascimento", dt.date(1956, 7, 15)),
        cd_cargo=cargo,
        ds_cargo=cargos.CARGO_NAMES[cargo],
        sg_uf=uf,
        sg_partido=over.pop("sg_partido", "PL"),
        ds_sit_tot_turno=sit,
        is_majoritario=cargos.is_majoritario(cargo),
        **over,
    )
    session.add(row)
    session.flush()
    return row


# ── The office taxonomy ──────────────────────────────────────────────────────
def test_governador_is_mapped_to_a_house_so_reelection_can_be_recognised():
    """The regression in one line: with GOVERNADOR absent from CARGO_HOUSE, no
    governor could ever be flagged as seeking re-election, whatever the data said."""
    assert cargos.house_for(cargos.GOVERNADOR) is House.EXECUTIVO
    assert House.EXECUTIVO.is_legislative is False
    # The three legislative houses must not be caught by the same predicate.
    for house in (House.CAMARA, House.SENADO, House.ASSEMBLEIA):
        assert house.is_legislative is True


def test_executive_term_runs_from_the_january_after_the_election():
    assert cargos.executive_term(2022) == (dt.date(2023, 1, 1), dt.date(2026, 12, 31))
    assert cargos.previous_general_election(2026) == 2022


# ── Roster collector ─────────────────────────────────────────────────────────
def test_governadores_seeds_a_mandate_from_the_winning_candidacy(session):
    _candidacy(session, "240001611127", ano=2022, nr_turno=2)
    # A losing candidacy in the same race must not become a mandate.
    _candidacy(
        session, "240001679805", ano=2022, sit="NÃO ELEITO",
        cpf_raw="11268786934", nome_candidato="ESPERIDIAO AMIN",
        nome_urna="ESPERIDIÃO AMIN", nome_normalizado="ESPERIDIAO AMIN", sg_partido="PP",
    )

    result = GovernadoresCollector().run(session, year=2022, ufs=["SC"])
    assert result.status == "ingested"
    assert result.row_count == 1

    mandate = session.execute(
        select(Mandate).where(Mandate.house == House.EXECUTIVO)
    ).scalar_one()
    assert mandate.house_member_id == "240001611127"
    assert mandate.sigla_uf == "SC"
    assert mandate.nome_parlamentar == "JORGINHO MELLO"
    assert mandate.data_inicio == dt.date(2023, 1, 1)
    # 🚨 The whole point. The term's end is known and in the future, but writing it
    # here would mark the seat vacant and undo the incumbency.
    assert mandate.data_fim is None
    # `id_legislatura` carries the term's start year for an executive mandate.
    assert mandate.id_legislatura == 2023

    person = session.get(Person, mandate.person_id)
    # CPF is what buys the `cpf_exact` tier downstream; losing it would drop the link
    # to a name-only match that can never reach auto_strong.
    assert person.cpf == CPF


def test_governadores_reuses_the_person_a_previous_mandate_created(session):
    """A governor who was a senator first must not be split into two people."""
    person = Person(cpf=CPF, nome_civil="JORGINHO DOS SANTOS MELLO")
    session.add(person)
    session.flush()
    session.add(
        Mandate(
            house=House.SENADO, house_member_id="5350", id_legislatura=56,
            person_id=person.id, nome_parlamentar="Jorginho Mello", sigla_uf="SC",
            data_inicio=dt.date(2019, 2, 1), data_fim=dt.date(2022, 12, 29),
        )
    )
    session.flush()

    _candidacy(session, "240001611127", ano=2022, nr_turno=2)
    GovernadoresCollector().run(session, year=2022, ufs=["SC"])

    assert session.execute(select(Person).where(Person.cpf == CPF)).scalars().all() == [person]
    houses = {
        m.house for m in session.execute(
            select(Mandate).where(Mandate.person_id == person.id)
        ).scalars()
    }
    assert houses == {House.SENADO, House.EXECUTIVO}


def test_governadores_is_idempotent(session):
    _candidacy(session, "240001611127", ano=2022, nr_turno=2)
    for _ in range(2):
        GovernadoresCollector().run(session, year=2022, ufs=["SC"])
    assert session.scalar(
        select(Mandate).where(Mandate.house == House.EXECUTIVO).exists().select()
    )
    assert len(session.execute(select(Mandate)).scalars().all()) == 1


def test_governadores_says_what_to_run_when_the_tse_rows_are_not_in_base(session):
    result = GovernadoresCollector().run(session, year=2022, ufs=["SC"])
    assert result.status == "empty"
    assert "tse-candidates" in result.detail


# ── Acts collector ───────────────────────────────────────────────────────────
# Trimmed from the live listing (verified 2026-08-28): one bill of executive
# initiative and one mensagem de veto, which is the pair that matters.
ATOS_PAGE = """
<p>Exibindo 1 - 2 de 2</p>
<div class="card card-alesc mb-3"><div class="card-body">
  <h4 class="card-title"><a href="/proposicoes/K4YxO">MSV/2042/2026</a></h4>
  <p class="mb-1 fst-italic">Veto Total ao Projeto de Lei nº 0287/2026.</p>
  <div>
    <div class="row"><div class="col-lg-2 fw-bold">Entrada</div>
      <div class="col-lg-10">06/08/2026</div></div>
    <div class="row"><div class="col-lg-2 fw-bold">Autoria</div>
      <div class="col-lg-10"><ul><li>Governador do Estado</li></ul></div></div>
    <div class="row"><div class="col-lg-2 fw-bold">Situação atual</div>
      <div class="col-lg-10">Aguardando apreciação pela Comissão</div></div>
  </div>
</div></div>
<div class="card card-alesc mb-3"><div class="card-body">
  <h4 class="card-title"><a href="/proposicoes/zLgAk">PL./0494/2026</a></h4>
  <p class="mb-1 fst-italic">Altera o art. 6º da Lei Complementar nº 774, de 2021.</p>
  <div>
    <div class="row"><div class="col-lg-2 fw-bold">Entrada</div>
      <div class="col-lg-10">13/07/2026</div></div>
    <div class="row"><div class="col-lg-2 fw-bold">Autoria</div>
      <div class="col-lg-10"><ul><li>Governador do Estado</li></ul></div></div>
    <div class="row"><div class="col-lg-2 fw-bold">Situação atual</div>
      <div class="col-lg-10">Arquivado</div></div>
  </div>
</div></div>
"""

# The shape a silently-ignored `iniciativa` produces: the whole Assembly, authored by
# somebody else entirely.
CASA_INTEIRA = """
<p>Exibindo 1 - 35027 de 35.027</p>
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


def _seed_mandate(session) -> Mandate:
    person = Person(cpf=CPF, nome_civil="JORGINHO DOS SANTOS MELLO")
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=House.EXECUTIVO, house_member_id="240001611127", id_legislatura=2023,
        person_id=person.id, nome_parlamentar="JORGINHO MELLO", sigla_uf="SC",
        data_inicio=dt.date(2023, 1, 1), data_fim=None, situacao="Exercício",
    )
    session.add(mandate)
    session.flush()
    return mandate


@respx.mock
def test_atos_collects_bills_and_vetoes_against_the_governors_mandate(session):
    mandate = _seed_mandate(session)
    # The sentinel request (no `iniciativa`) and the filtered crawl differ, so the
    # unfiltered guard must not fire.
    respx.get(url__startswith=f"{ELEGIS}/proposicoes/processo-legislativo").mock(
        side_effect=lambda request: httpx.Response(
            200,
            text=CASA_INTEIRA if "iniciativa" not in request.url.params else ATOS_PAGE,
        )
    )

    result = AtosCollector().run(session, year=2022)
    assert result.status == "ingested"
    assert result.row_count == 2

    rows = {
        p.sigla_tipo: p
        for p in session.execute(select(Proposition)).scalars()
    }
    assert set(rows) == {"MSV", "PL."}
    veto = rows["MSV"]
    assert veto.house is House.EXECUTIVO
    assert veto.authoring_mandate_id == mandate.id
    assert veto.numero == 2042
    assert veto.data_apresentacao == dt.date(2026, 8, 6)
    # Prefixed so an e-Legis hashid can never collide with a Câmara numeric id.
    assert veto.proposition_id == "ALK4YxO"


@respx.mock
def test_atos_refuses_the_unfiltered_listing(session, caplog):
    """e-Legis answers an unrecognised `iniciativa` with the whole Assembly instead of
    an error. Attributing that to one person would put 35.027 bills on their ficha."""
    _seed_mandate(session)
    respx.get(url__startswith=f"{ELEGIS}/proposicoes/processo-legislativo").mock(
        return_value=httpx.Response(200, text=CASA_INTEIRA)
    )

    with caplog.at_level("ERROR"):
        result = AtosCollector().run(session, year=2022)

    assert result.row_count == 0
    assert session.execute(select(Proposition)).scalars().all() == []
    assert "SEM filtro" in caplog.text


@respx.mock
def test_atos_skips_a_card_authored_by_someone_else(session):
    """The listing filter having applied does not promise every row on it is the
    governor's — each card asserts its own authorship."""
    _seed_mandate(session)
    mixed = ATOS_PAGE + CASA_INTEIRA.split("<p>Exibindo 1 - 35027 de 35.027</p>")[1]
    respx.get(url__startswith=f"{ELEGIS}/proposicoes/processo-legislativo").mock(
        side_effect=lambda request: httpx.Response(
            200, text=CASA_INTEIRA if "iniciativa" not in request.url.params else mixed
        )
    )

    result = AtosCollector().run(session, year=2022)
    assert result.row_count == 2
    assert "outra autoria" in result.detail
    siglas = {p.sigla_tipo for p in session.execute(select(Proposition)).scalars()}
    assert siglas == {"MSV", "PL."}  # Ana Campagnolo's bill is not among them


def test_atos_says_what_to_run_when_no_executive_mandate_exists(session):
    result = AtosCollector().run(session, year=2022)
    assert result.status == "empty"
    assert "executivo-governadores" in result.detail


# ── End to end: the badge ────────────────────────────────────────────────────
def test_sitting_governor_seeking_a_second_term_is_flagged_as_reelection(session):
    """The original report, start to finish: seed both TSE rows, seed the mandate,
    resolve, and read what the listing renders."""
    _candidacy(session, "240001611127", ano=2022, nr_turno=2)
    _candidacy(session, "240002537073", ano=2026, sit=None)
    GovernadoresCollector().run(session, year=2022, ufs=["SC"])
    resolve(session, year=2026)

    link = session.execute(
        select(CandidateMandateLink).where(
            CandidateMandateLink.sq_candidato == "240002537073"
        )
    ).scalar_one()
    assert link.is_incumbent_reelection is True
    assert link.confidence_tier is ConfidenceTier.auto_strong

    (row,) = queries.search_candidacies(session, cargo="GOVERNADOR", year=2026)
    assert row["incumbent_confirmed"] is True
    assert row["incumbent_house"] == "Governo do Estado"
    # Same office -> "tentando reeleição", not the "mandato atual · X" fallback.
    assert row["reelection_same_office"] is True


def test_a_former_senator_governorship_aside_is_not_an_incumbent_senator(session):
    """The false positive the fix must not introduce. An expired Senate term is not
    a current mandate, however cleanly it matches."""
    person = Person(cpf=CPF, nome_civil="JORGINHO DOS SANTOS MELLO")
    session.add(person)
    session.flush()
    session.add(
        Mandate(
            house=House.SENADO, house_member_id="5350", id_legislatura=56,
            person_id=person.id, nome_parlamentar="Jorginho Mello", sigla_uf="SC",
            data_inicio=dt.date(2019, 2, 1), data_fim=dt.date(2022, 12, 29),
        )
    )
    session.flush()
    _candidacy(session, "240002537073", ano=2026, sit=None)

    resolve(session, year=2026)

    link = session.execute(select(CandidateMandateLink)).scalar_one()
    assert link.is_incumbent_reelection is False
    (row,) = queries.search_candidacies(session, cargo="GOVERNADOR", year=2026)
    assert row["incumbent_confirmed"] is False


# ── The ficha must not print legislative counters for an executive ───────────
@pytest.mark.parametrize("secao", ["votos", "gastos"])
def test_counters_the_office_cannot_have_are_no_page_at_all(session, secao):
    """Not an empty listing — no URL. "0 votos nominais" for a governor reads as a
    collection failure, and a reader cannot tell it from one."""
    _candidacy(session, "240001611127", ano=2022, nr_turno=2)
    _candidacy(session, "240002537073", ano=2026, sit=None)
    GovernadoresCollector().run(session, year=2022, ufs=["SC"])
    resolve(session, year=2026)

    assert queries.track_section(session, "240002537073", secao) is None
    # Proposições, by contrast, exists and is renamed for what it actually holds.
    section = queries.track_section(session, "240002537073", "proposicoes")
    assert section is not None
    assert section["titulo"] == "Projetos de iniciativa do Executivo e vetos"


def test_track_record_payload_flags_the_mandate_as_non_legislative(session):
    _candidacy(session, "240001611127", ano=2022, nr_turno=2)
    _candidacy(session, "240002537073", ano=2026, sit=None)
    GovernadoresCollector().run(session, year=2022, ufs=["SC"])
    resolve(session, year=2026)

    detail = queries.candidate_detail(session, "240002537073")
    assert detail["link"]["is_legislative"] is False
    assert detail["link"]["same_office"] is True
    assert detail["track_record"]["is_legislative"] is False
    # `partial`, so the ficha still renders the section — with its caveat.
    assert detail["candidacy"]["history_status"] == "partial"
    assert cargos.house_caveat(House.EXECUTIVO)
