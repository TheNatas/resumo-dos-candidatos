"""Frequência parlamentar: relatório oficial da Câmara, resumos derivados, licenças.

Os fixtures HTML são recortes reais do portal da Câmara (verificados ao vivo em
2026-08-19) e mantêm as armadilhas que motivaram cada decisão do parser: a tabela
do diário com ``rowspan`` antes da tabela-resumo, o asterisco da nota de rodapé
grudado no rótulo, e a página de "não há dados" que **não** é zero falta.

O eixo destes testes é sempre o mesmo: **a unidade da fonte é a unidade exibida**.
Nenhum número é convertido de sessão para dia ou vice-versa.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select

from resumo import attendance as att
from resumo.api.queries import attendance_payload, leaves_payload
from resumo.db.models import (
    AttendanceRecord,
    AttendanceSummary,
    AttendanceUnit,
    House,
    Mandate,
    MandateLeave,
)
from resumo.ingestion.attendance_summary import AttendanceSummaryCollector
from resumo.ingestion.camara.parsing import CamaraParseError, parse_presenca_plenario
from resumo.ingestion.camara.presenca_plenario import PresencaPlenarioCollector, default_years
from resumo.ingestion.senado.licencas import LicencasCollector

PORTAL = "https://www.camara.leg.br"
SENADO = "https://legis.senado.leg.br/dadosabertos"


# ── Fixtures de HTML ─────────────────────────────────────────────────────────
# A página real traz DUAS tabelas com a mesma classe: primeiro o diário (com
# rowspan, que faz um parser ingênuo contar 291 "Presença" para 96 dias), depois o
# resumo da Mesa. O fixture mantém as duas para provar que a última é a lida.
def _relatorio(
    *,
    sessoes_total: int = 104,
    sessoes_ausencia: int = 4,
    dias_total: int = 100,
    dias_presenca: int = 96,
    dias_justificada: int = 1,
    dias_nao_justificada: int = 3,
) -> str:
    return f"""
    <html><body>
      <table class="table table-bordered">
        <tbody>
          <tr>
            <td rowspan="2"><span>09/12/2025</span></td>
            <td>EXTRAORDINÁRIA Nº 277 - 09/12/2025</td>
            <td>Ausência</td>
            <td rowspan="2" class="info-presenca-dia">Presença</td>
          </tr>
          <tr>
            <td>EXTRAORDINÁRIA Nº 278 - 09/12/2025</td>
            <td>Presença</td>
          </tr>
        </tbody>
      </table>
      <table class="table table-bordered">
        <tbody>
          <tr class="info-data"><td><strong>2025</strong></td><td></td><td></td></tr>
          <tr>
            <td><b>Total de sessões deliberativas com Ordem do Dia iniciada, na Sessão Legislativa*</b></td>
            <td><strong>{sessoes_total}</strong></td><td><strong>100,00%</strong></td>
          </tr>
          <tr>
            <td><b>Total de ausências não justificadas em sessões deliberativas com Ordem do Dia iniciada*</b></td>
            <td><strong>{sessoes_ausencia}</strong></td><td><strong>3,85%</strong></td>
          </tr>
          <tr><td colspan="3"></td></tr>
          <tr>
            <td><b>Total de dias com sessões deliberativas realizadas no período</b></td>
            <td>{dias_total}</td><td>100,00%</td>
          </tr>
          <tr>
            <td><b>Total de dias com presença nas sessões deliberativas</b></td>
            <td>{dias_presenca}</td><td>96,00%</td>
          </tr>
          <tr>
            <td><b>Total de dias com ausências justificadas em sessões deliberativas</b></td>
            <td>{dias_justificada}</td><td>1,00%</td>
          </tr>
          <tr>
            <td><b>Total de dias com ausências não justificadas em sessões deliberativas</b></td>
            <td>{dias_nao_justificada}</td><td>3,00%</td>
          </tr>
        </tbody>
      </table>
      <div>* Ato da Mesa n. 191 de 2017</div>
      <div>Obs.: No período de exercício no mandato do parlamentar.</div>
    </body></html>
    """


# Ano fora do exercício do parlamentar: HTTP 200, sem tabela, com o aviso.
SEM_DADOS = """
<html><body>
  <h1>Presença em Plenário - 2019</h1>
  <p>Não há dados disponíveis para o ano de 2019.</p>
</body></html>
"""


def _seed_mandate(session, member_id: str, house: House = House.CAMARA, leg: int = 57) -> Mandate:
    mandate = Mandate(
        house=house,
        house_member_id=member_id,
        id_legislatura=leg,
        nome_parlamentar=f"PARLAMENTAR {member_id}",
        sigla_uf="SC",
    )
    session.add(mandate)
    session.flush()
    return mandate


# ── 1. Parser do relatório oficial ───────────────────────────────────────────
def test_parser_reads_the_summary_table_not_the_daily_one():
    """O diário tem rowspan e infla qualquer contagem feita linha a linha; os seis
    números da Mesa saem da ÚLTIMA tabela."""
    r = parse_presenca_plenario(_relatorio(), ano=2025)

    assert (r.dias_total, r.dias_presenca) == (100, 96)
    assert (r.dias_ausencia_justificada, r.dias_ausencia_nao_justificada) == (1, 3)
    assert (r.sessoes_total, r.sessoes_ausencia_nao_justificada) == (104, 4)


def test_parser_keeps_the_two_rulers_apart():
    """104 sessões e 100 dias no mesmo ano não são um erro: são réguas diferentes,
    e a presença de cada uma sai do próprio denominador."""
    r = parse_presenca_plenario(_relatorio(), ano=2025)

    assert r.dias_presenca_efetiva == 96  # 96 de 100 dias
    assert r.sessoes_presenca == 100  # 104 sessões menos 4 ausências não justificadas


def test_ano_sem_dados_is_none_never_zero():
    """"Não há dados" significa "não estava em exercício", não "não faltou". Um
    resumo zerado ali afirmaria presença perfeita num ano que não existiu."""
    assert parse_presenca_plenario(SEM_DADOS, ano=2019) is None


def test_layout_drift_raises_instead_of_writing_zeros():
    drift = '<html><table class="table table-bordered"><tr><td>Outro rótulo</td><td>7</td></tr></table></html>'
    with pytest.raises(CamaraParseError):
        parse_presenca_plenario(drift, ano=2025)


def test_default_years_cover_the_legislature_ending_in_the_election():
    assert default_years(2026) == [2023, 2024, 2025, 2026]


# ── 2. Coletor da Câmara ─────────────────────────────────────────────────────
@respx.mock
def test_camara_collector_writes_one_row_per_ruler(session):
    mandate = _seed_mandate(session, "204528")
    respx.get(url__regex=rf"{PORTAL}/deputados/204528/presenca-plenario/2025").mock(
        return_value=httpx.Response(200, text=_relatorio())
    )

    res = PresencaPlenarioCollector().run(session, anos=[2025])
    session.flush()

    rows = {
        r.unidade: r
        for r in session.scalars(
            select(AttendanceSummary).where(AttendanceSummary.mandate_id == mandate.id)
        )
    }
    assert set(rows) == {AttendanceUnit.DIA, AttendanceUnit.SESSAO}
    assert res.row_count == 2

    dia = rows[AttendanceUnit.DIA]
    assert (dia.total, dia.presenca) == (100, 96)
    assert (dia.ausencia_justificada, dia.ausencia_nao_justificada) == (1, 3)
    assert dia.metrica == att.CAMARA_PLENARIO
    assert dia.source_url.endswith("/deputados/204528/presenca-plenario/2025")

    sessao = rows[AttendanceUnit.SESSAO]
    assert (sessao.total, sessao.presenca) == (104, 100)
    assert sessao.ausencia_nao_justificada == 4
    # A Mesa não publica ausência JUSTIFICADA por sessão — só por dia. Importar o
    # número da outra régua misturaria as duas contagens.
    assert sessao.ausencia_justificada is None


@respx.mock
def test_camara_collector_skips_years_outside_the_mandate(session):
    _seed_mandate(session, "220639")
    respx.get(url__regex=rf"{PORTAL}/deputados/220639/presenca-plenario/2019").mock(
        return_value=httpx.Response(200, text=SEM_DADOS)
    )
    respx.get(url__regex=rf"{PORTAL}/deputados/220639/presenca-plenario/2025").mock(
        return_value=httpx.Response(200, text=_relatorio())
    )

    res = PresencaPlenarioCollector().run(session, anos=[2019, 2025])
    session.flush()

    anos = {r.ano for r in session.scalars(select(AttendanceSummary))}
    assert anos == {2025}
    assert "sem dados" in res.detail


@respx.mock
def test_camara_collector_is_idempotent(session):
    _seed_mandate(session, "204528")
    respx.get(url__regex=rf"{PORTAL}/deputados/204528/presenca-plenario/2025").mock(
        return_value=httpx.Response(200, text=_relatorio())
    )

    PresencaPlenarioCollector().run(session, anos=[2025])
    PresencaPlenarioCollector().run(session, anos=[2025])
    session.flush()

    assert len(session.scalars(select(AttendanceSummary)).all()) == 2


@respx.mock
def test_a_broken_page_does_not_kill_the_run(session):
    """O portal responde 500 (não 404) para id inexistente, e uma página instável não
    pode derrubar a coleta dos outros deputados."""
    _seed_mandate(session, "111")
    _seed_mandate(session, "222")
    respx.get(url__regex=rf"{PORTAL}/deputados/111/presenca-plenario/2025").mock(
        return_value=httpx.Response(500)
    )
    respx.get(url__regex=rf"{PORTAL}/deputados/222/presenca-plenario/2025").mock(
        return_value=httpx.Response(200, text=_relatorio())
    )

    res = PresencaPlenarioCollector().run(session, anos=[2025])
    session.flush()

    membros = {r.house_member_id for r in session.scalars(select(AttendanceSummary))}
    assert membros == {"222"}
    assert "falha" in res.detail


# ── 3. Resumos derivados (Senado e ALESC) ────────────────────────────────────
def _attendance(session, mandate, *, id_evento, data, presente, justificativa, derivation):
    session.add(
        AttendanceRecord(
            mandate_id=mandate.id,
            house_member_id=mandate.house_member_id,
            id_evento=id_evento,
            data=data,
            tipo="Sessão",
            presente=presente,
            justificativa=justificativa,
            derivation=derivation,
        )
    )


def test_derived_summary_never_claims_an_absence_is_unjustified(session):
    """Nem o Senado nem a ALESC dizem que uma falta foi INJUSTIFICADA. "Não
    compareceu" é ausência sem classificação; só a Câmara publica essa distinção."""
    mandate = _seed_mandate(session, "5012", house=House.SENADO)
    d = "senado_votacao_comparecimento"
    _attendance(session, mandate, id_evento="SF1", data=dt.date(2025, 3, 1),
                presente=True, justificativa=None, derivation=d)
    _attendance(session, mandate, id_evento="SF2", data=dt.date(2025, 3, 2),
                presente=False, justificativa=None, derivation=d)  # NCom
    _attendance(session, mandate, id_evento="SF3", data=dt.date(2025, 3, 3),
                presente=False, justificativa="Missão política", derivation=d)  # MIS
    session.flush()

    res = AttendanceSummaryCollector(d).run(session)
    session.flush()

    row = session.scalars(select(AttendanceSummary)).one()
    assert res.row_count == 1
    assert row.unidade is AttendanceUnit.SESSAO  # a régua do Senado é a sessão
    assert (row.total, row.presenca) == (3, 1)
    assert row.ausencia_justificada == 1
    assert row.ausencia_nao_classificada == 1
    assert row.ausencia_nao_justificada is None
    assert row.metrica == att.SENADO_VOTACAO_NOMINAL


def test_unknown_codes_stay_out_of_the_denominator(session):
    """`presente IS NULL` é um código que a própria fonte não define. Contá-lo como
    presença infla o histórico; como ausência, inventa uma falta."""
    mandate = _seed_mandate(session, "5012", house=House.SENADO)
    d = "senado_votacao_comparecimento"
    _attendance(session, mandate, id_evento="SF1", data=dt.date(2025, 3, 1),
                presente=True, justificativa=None, derivation=d)
    _attendance(session, mandate, id_evento="SF2", data=dt.date(2025, 3, 2),
                presente=None, justificativa="Dispositivo não citado", derivation=d)
    session.flush()

    AttendanceSummaryCollector(d).run(session)
    session.flush()

    row = session.scalars(select(AttendanceSummary)).one()
    assert (row.total, row.presenca) == (1, 1)


def test_derived_summary_splits_by_year(session):
    mandate = _seed_mandate(session, "ana-campagnolo", house=House.ASSEMBLEIA, leg=20)
    d = "alesc_sessao_presenca"
    _attendance(session, mandate, id_evento="AL1", data=dt.date(2024, 5, 2),
                presente=True, justificativa=None, derivation=d)
    _attendance(session, mandate, id_evento="AL2", data=dt.date(2025, 5, 2),
                presente=False, justificativa="Ausência justificada", derivation=d)
    session.flush()

    AttendanceSummaryCollector(d).run(session)
    session.flush()

    rows = {r.ano: r for r in session.scalars(select(AttendanceSummary))}
    assert set(rows) == {2024, 2025}
    assert rows[2025].ausencia_justificada == 1
    assert rows[2024].presenca == 1
    assert all(r.metrica == att.ALESC_SESSAO_PLENARIA for r in rows.values())


def test_camara_records_are_not_summarizable(session):
    """`attendance_record` só recebe presenças da Câmara (o endpoint de eventos não
    devolve ausentes); agregá-las daria 100% para todo deputado federal."""
    with pytest.raises(ValueError, match="camara_evento_presenca"):
        AttendanceSummaryCollector("camara_evento_presenca")


def test_records_without_a_date_are_reported_not_dropped_silently(session):
    mandate = _seed_mandate(session, "5012", house=House.SENADO)
    d = "senado_votacao_comparecimento"
    _attendance(session, mandate, id_evento="SF1", data=dt.date(2025, 3, 1),
                presente=True, justificativa=None, derivation=d)
    _attendance(session, mandate, id_evento="SF2", data=None,
                presente=False, justificativa=None, derivation=d)
    session.flush()

    res = AttendanceSummaryCollector(d).run(session)
    assert "sem data" in res.detail


# ── 4. Licenças do Senado ────────────────────────────────────────────────────
def _licencas(codigo: str, licencas: list[dict]) -> dict:
    # Uma licença só chega como objeto; várias, como lista (colapso do XML→JSON).
    node = licencas[0] if len(licencas) == 1 else licencas
    return {
        "LicencaParlamentar": {
            "Parlamentar": {"Codigo": codigo, "Nome": "Fulano", "Licencas": {"Licenca": node}}
        }
    }


@respx.mock
def test_licencas_collector_stores_days_with_the_published_reason(session):
    mandate = _seed_mandate(session, "4981", house=House.SENADO)
    respx.get(f"{SENADO}/senador/4981/licencas").mock(
        return_value=httpx.Response(
            200,
            json=_licencas(
                "4981",
                [
                    {
                        "Codigo": "14390",
                        "DataInicio": "2022-12-14",
                        "DataFim": "2022-12-14",
                        "SiglaTipoAfastamento": "LICENCA_ATIVIDADE_PARLAMENTAR",
                        "DescricaoTipoAfastamento": "Missão política ou cultural",
                    },
                    {
                        "Codigo": "14391",
                        "DataInicio": "2025-03-01",
                        "DataFim": "2025-03-10",
                        "SiglaTipoAfastamento": "LICENCA_SAUDE",
                        "DescricaoTipoAfastamento": "Licença para tratamento de saúde",
                    },
                ],
            ),
        )
    )

    res = LicencasCollector().run(session)
    session.flush()

    rows = {r.leave_id: r for r in session.scalars(select(MandateLeave))}
    assert set(rows) == {"14390", "14391"}
    assert rows["14390"].mandate_id == mandate.id
    assert rows["14390"].descricao_tipo == "Missão política ou cultural"
    # Uma licença de 14/12 a 14/12 é UM dia, não zero.
    assert "11 dia" in res.detail  # 1 + 10


@respx.mock
def test_a_senator_without_leaves_is_normal_not_an_error(session):
    _seed_mandate(session, "5012", house=House.SENADO)
    # Sem licença, o serviço simplesmente não emite o nó `Licencas`.
    respx.get(f"{SENADO}/senador/5012/licencas").mock(
        return_value=httpx.Response(
            200, json={"LicencaParlamentar": {"Parlamentar": {"Codigo": "5012"}}}
        )
    )

    res = LicencasCollector().run(session)
    session.flush()

    assert session.scalars(select(MandateLeave)).all() == []
    assert res.status == "empty"
    assert "sem licença" in res.detail


def test_leave_days_counts_both_ends():
    assert att.leave_days(dt.date(2022, 12, 14), dt.date(2022, 12, 14)) == 1
    assert att.leave_days(dt.date(2025, 3, 1), dt.date(2025, 3, 10)) == 10
    assert att.leave_days(None, dt.date(2025, 3, 10)) is None


# ── 5. O que a ficha pública mostra ──────────────────────────────────────────
def _summary(session, mandate, **over):
    row = {
        "mandate_id": mandate.id,
        "house": mandate.house,
        "house_member_id": mandate.house_member_id,
        "ano": 2025,
        "ambito": att.AMBITO_PLENARIO,
        "unidade": AttendanceUnit.DIA,
        "total": 100,
        "presenca": 96,
        "ausencia_justificada": 1,
        "ausencia_nao_justificada": 3,
        "ausencia_nao_classificada": None,
        "metrica": att.CAMARA_PLENARIO,
        "derivation": "camara_presenca_plenario_oficial",
        "source_url": f"{PORTAL}/deputados/1/presenca-plenario/2025",
    }
    row.update(over)
    session.add(AttendanceSummary(**row))
    session.flush()


def test_payload_labels_each_number_with_its_own_unit(session):
    mandate = _seed_mandate(session, "204528")
    _summary(session, mandate)
    _summary(
        session, mandate, unidade=AttendanceUnit.SESSAO, total=104, presenca=100,
        ausencia_justificada=None, ausencia_nao_justificada=4,
    )

    payload = attendance_payload(session, mandate.id)

    assert payload["available"] is True
    assert [r["unidade"] for r in payload["rows"]] == ["DIA", "SESSAO"]
    dia, sessao = payload["rows"]
    assert (dia["unidade_label"], dia["presenca"], dia["total"]) == ("dias", 96, 100)
    assert dia["denominador"] == "dias com sessão deliberativa realizada"
    assert dia["percentual_presenca"] == 96.0
    assert (sessao["unidade_label"], sessao["presenca"], sessao["total"]) == ("sessões", 100, 104)
    assert sessao["denominador"] == "sessões deliberativas com Ordem do Dia iniciada"
    assert payload["derivada"] is False


def test_payload_sums_years_inside_each_ruler(session):
    mandate = _seed_mandate(session, "204528")
    _summary(session, mandate, ano=2024, total=87, presenca=87,
             ausencia_justificada=0, ausencia_nao_justificada=0)
    _summary(session, mandate, ano=2025)

    dia = attendance_payload(session, mandate.id)["rows"][0]

    assert (dia["total"], dia["presenca"]) == (187, 183)
    assert dia["anos"] == [2024, 2025]


def test_a_source_that_does_not_classify_absence_reports_none_not_zero(session):
    """Zero e "a fonte não publica isso" são coisas diferentes, e a diferença é
    pública: a ALESC nunca diz que uma falta foi injustificada."""
    mandate = _seed_mandate(session, "ana-campagnolo", house=House.ASSEMBLEIA, leg=20)
    _summary(
        session, mandate, unidade=AttendanceUnit.SESSAO, metrica=att.ALESC_SESSAO_PLENARIA,
        derivation="alesc_sessao_presenca", total=40, presenca=38,
        ausencia_justificada=2, ausencia_nao_justificada=None, ausencia_nao_classificada=0,
    )

    row = attendance_payload(session, mandate.id)["rows"][0]

    assert row["ausencia_justificada"] == 2
    assert row["ausencia_nao_justificada"] is None
    assert row["unidade_label"] == "sessões"


def test_no_summary_means_no_attendance_block(session):
    """Sem denominador publicado não há percentual honesto — e a ficha prefere o
    silêncio explicado a um "100%" que só reflete o que nós coletamos."""
    mandate = _seed_mandate(session, "999")
    session.add(
        AttendanceRecord(
            mandate_id=mandate.id, house_member_id="999", id_evento="1",
            data=dt.date(2025, 3, 1), presente=True, derivation="camara_evento_presenca",
        )
    )
    session.flush()

    payload = attendance_payload(session, mandate.id)

    assert payload["available"] is False
    assert payload["rows"] == []


def test_leaves_payload_is_none_when_there_are_none(session):
    mandate = _seed_mandate(session, "5012", house=House.SENADO)
    assert leaves_payload(session, mandate.id) is None


def test_leaves_payload_totals_calendar_days(session):
    mandate = _seed_mandate(session, "4981", house=House.SENADO)
    session.add_all(
        [
            MandateLeave(
                mandate_id=mandate.id, house=House.SENADO, house_member_id="4981",
                leave_id="1", data_inicio=dt.date(2025, 3, 1), data_fim=dt.date(2025, 3, 10),
                descricao_tipo="Licença para tratamento de saúde",
            ),
            MandateLeave(
                mandate_id=mandate.id, house=House.SENADO, house_member_id="4981",
                leave_id="2", data_inicio=dt.date(2025, 6, 1), data_fim=dt.date(2025, 6, 1),
                descricao_tipo="Missão política ou cultural",
            ),
        ]
    )
    session.flush()

    payload = leaves_payload(session, mandate.id)

    assert payload["count"] == 2
    assert payload["dias"] == 11
    assert payload["tipos"][0]["count"] == 1


# ── 6. A página publicada ────────────────────────────────────────────────────
def _render_candidate(sq: str) -> str:
    """A ficha como o leitor a recebe, pelo app real (rota, template e tudo)."""
    from fastapi.testclient import TestClient

    from resumo.api.main import app

    resp = TestClient(app).get(f"/candidato/{sq}")
    assert resp.status_code == 200
    return resp.text


def _seed_incumbent(session, *, cd_cargo: int = 6, house: House = House.CAMARA) -> Mandate:
    from resumo.db.models import (
        Candidacy,
        CandidateMandateLink,
        ConfidenceTier,
        MatchMethod,
        Person,
    )

    person = Person(cpf="12345678909", nome_normalizado="JOSE DA SILVA")
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=house, house_member_id="1", id_legislatura=57, person_id=person.id,
        sigla_uf="SC", nome_parlamentar="JOSE",
    )
    session.add(mandate)
    session.flush()
    session.add(
        Candidacy(
            sq_candidato="C1", ano_eleicao=2026, sg_uf="SC", cd_cargo=cd_cargo,
            ds_cargo="DEPUTADO FEDERAL", nome_candidato="JOSE DA SILVA", nome_urna="JOSE",
            nome_normalizado="JOSE DA SILVA", sg_partido="PT", is_majoritario=False,
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
    session.commit()
    return mandate


def test_page_prints_days_as_days_and_sessions_as_sessions(session):
    """O teste que resume a regra: a ficha usa o substantivo da fonte. A Câmara
    publica as duas réguas, e as duas saem rotuladas — nenhuma vira a outra."""
    mandate = _seed_incumbent(session)
    _summary(session, mandate)
    _summary(
        session, mandate, unidade=AttendanceUnit.SESSAO, total=104, presenca=100,
        ausencia_justificada=None, ausencia_nao_justificada=4,
    )
    session.commit()

    html = _render_candidate("C1")

    assert "96/100" in html and "dias com presença" in html
    assert "100/104" in html and "sessões com presença" in html
    # Cada linha carrega o substantivo da própria régua, e as duas convivem.
    flat = " ".join(html.split())
    assert "<strong>3</strong> <span>dias de ausência não justificada</span>" in flat
    assert "<strong>4</strong> <span>sessões de ausência não justificada</span>" in flat
    assert "<strong>1</strong> <span>dias de ausência justificada</span>" in flat
    # O rótulo antigo — uma razão de eventos coletados, 100% para todo deputado — some.
    assert "presenças*" not in html


def test_page_says_why_it_is_silent_when_there_is_no_denominator(session):
    _seed_incumbent(session)

    html = _render_candidate("C1")

    assert "Sem frequência consolidada" in html
    assert "não sobre o que era devido" in html


@respx.mock
def test_licencas_from_earlier_mandates_are_not_attached_to_this_one(session):
    """🚨 `/licencas` devolve a carreira inteira: Acir Gurgacz volta a 2009, três
    mandatos atrás. Somá-las ao mandato atual atribuiria a ele licenças de dez anos
    antes num bloco que fala da legislatura corrente."""
    mandate = _seed_mandate(session, "4981", house=House.SENADO)
    mandate.data_inicio = dt.date(2023, 2, 1)
    session.flush()
    respx.get(f"{SENADO}/senador/4981/licencas").mock(
        return_value=httpx.Response(
            200,
            json=_licencas(
                "4981",
                [
                    {"Codigo": "1", "DataInicio": "2011-04-28", "DataFim": "2011-05-10",
                     "DescricaoTipoAfastamento": "Missão política"},
                    {"Codigo": "2", "DataInicio": "2025-03-01", "DataFim": "2025-03-10",
                     "DescricaoTipoAfastamento": "Licença Saúde"},
                ],
            ),
        )
    )

    res = LicencasCollector().run(session)
    session.flush()

    rows = session.scalars(select(MandateLeave)).all()
    assert [r.leave_id for r in rows] == ["2"]
    assert "1 de mandatos anteriores" in res.detail


@respx.mock
def test_without_a_mandate_window_nothing_is_dropped_but_it_is_flagged(session, caplog):
    """Descartar em silêncio seria pior que exibir demais — mas o coletor precisa
    dizer que não conseguiu separar."""
    _seed_mandate(session, "4981", house=House.SENADO)  # sem data_inicio
    respx.get(f"{SENADO}/senador/4981/licencas").mock(
        return_value=httpx.Response(
            200,
            json=_licencas("4981", [
                {"Codigo": "1", "DataInicio": "2011-04-28", "DataFim": "2011-05-10"},
            ]),
        )
    )

    res = LicencasCollector().run(session)
    session.flush()

    assert len(session.scalars(select(MandateLeave)).all()) == 1
    assert "sem janela de mandato" in res.detail
