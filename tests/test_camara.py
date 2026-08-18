from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select
from tests.helpers import deputado_detail

from resumo.db.models import House, Mandate, Person, Vote
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.camara.deputados import DeputadosCollector
from resumo.ingestion.camara.votacoes import VotacoesCollector

BASE = "https://dadosabertos.camara.leg.br/api/v2"


@respx.mock
def test_paginate_follows_next_links():
    page1 = {
        "dados": [{"id": 1}, {"id": 2}],
        "links": [{"rel": "next", "href": f"{BASE}/deputados?pagina=2&itens=2"}],
    }
    page2 = {"dados": [{"id": 3}], "links": [{"rel": "self", "href": f"{BASE}/deputados?pagina=2"}]}

    def handler(request):
        return httpx.Response(200, json=page2 if request.url.params.get("pagina") == "2" else page1)

    respx.get(url__regex=r"/deputados\?").mock(side_effect=handler)

    with CamaraClient() as client:
        ids = [d["id"] for d in client.paginate("deputados", {"itens": 2})]
    assert ids == [1, 2, 3]


@respx.mock
def test_deputados_collector_seeds_person_and_mandate(session):
    respx.get(url__regex=r"/deputados\?").mock(
        return_value=httpx.Response(200, json={"dados": [{"id": 1, "nome": "JOSE"}], "links": []})
    )
    respx.get(url__regex=r"/deputados/1$").mock(
        return_value=httpx.Response(200, json=deputado_detail("1", "123.456.789-09", "JOSE DA SILVA"))
    )

    res = DeputadosCollector().run(session, id_legislatura=57)
    session.commit()

    assert res.row_count == 1
    person = session.scalar(select(Person).where(Person.cpf == "12345678909"))
    assert person is not None
    assert person.nome_normalizado == "JOSE DA SILVA"
    mandate = session.scalar(select(Mandate))
    assert mandate.person_id == person.id
    assert mandate.sigla_uf == "SC"
    assert mandate.house_member_id == "1"


# ── Janela de datas de /votacoes ─────────────────────────────────────────────
# A API responde 400 "A diferença entre as datas não pode ser maior que 3 meses".
# O limite é do endpoint, então o coletor fatia sozinho: o range do README
# (2026-01-01 → hoje) precisa funcionar sem que o caller saiba da regra.
import datetime as _dt  # noqa: E402

from resumo.ingestion.camara.votacoes import (  # noqa: E402
    _MAX_WINDOW_DAYS,
    date_windows,
)


def _days(a: str, b: str) -> int:
    return (_dt.date.fromisoformat(b) - _dt.date.fromisoformat(a)).days + 1


def test_date_windows_cover_the_range_without_gaps_or_overlap():
    windows = list(date_windows("2026-01-01", "2026-08-18"))

    assert windows[0][0] == "2026-01-01"
    assert windows[-1][1] == "2026-08-18"
    for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
        # O próximo dia exato: sem buraco (perderia votações) e sem sobreposição.
        assert _dt.date.fromisoformat(start) == _dt.date.fromisoformat(end) + _dt.timedelta(days=1)


def test_no_window_exceeds_the_api_limit():
    for start, end in date_windows("2023-01-01", "2026-12-31"):
        assert _days(start, end) <= _MAX_WINDOW_DAYS
    # E a folga é real: o limite observado da API fica bem acima de 80 dias.
    assert _MAX_WINDOW_DAYS <= 88


def test_short_and_degenerate_ranges():
    assert list(date_windows("2026-08-18", "2026-08-18")) == [("2026-08-18", "2026-08-18")]
    assert len(list(date_windows("2026-01-01", "2026-02-01"))) == 1
    # Fim antes do início não gera janela nenhuma, em vez de girar para sempre.
    assert list(date_windows("2026-08-18", "2026-01-01")) == []


# ── Votações: inconsistência da fonte não pode custar a coleta inteira ────────
def _seed_camara_mandate(session):
    person = Person(nome_normalizado="JOSE", nome_civil="JOSE")
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=House.CAMARA, house_member_id="1", id_legislatura=57,
        person_id=person.id, sigla_uf="SC", nome_parlamentar="JOSE",
    )
    session.add(mandate)
    session.commit()
    return mandate


def _votacoes_listing(ids):
    return {"dados": [{"id": i, "data": "2026-02-10"} for i in ids], "links": []}


@respx.mock
def test_votacao_missing_from_the_source_is_skipped_not_fatal(session):
    """A listagem devolve ids cujos endpoints de detalhe não existem. Isso derrubou
    a coleta inteira em produção: uma votação inconsistente apagou todas as outras."""
    _seed_camara_mandate(session)
    respx.get(url__regex=r"/votacoes\?").mock(
        return_value=httpx.Response(200, json=_votacoes_listing(["BOA-1", "SUMIDA-2"]))
    )
    respx.get(url__regex=r"/votacoes/[^/]+/orientacoes").mock(
        return_value=httpx.Response(200, json={"dados": []})
    )
    respx.get(url__regex=r"/votacoes/BOA-1/votos").mock(
        return_value=httpx.Response(
            200,
            json={"dados": [{"deputado_": {"id": 1, "siglaPartido": "PT"}, "tipoVoto": "Sim"}]},
        )
    )
    respx.get(url__regex=r"/votacoes/SUMIDA-2/votos").mock(return_value=httpx.Response(404))

    result = VotacoesCollector().run(
        session, data_inicio="2026-02-01", data_fim="2026-02-28"
    )

    # A votação boa foi guardada, a inconsistente foi pulada — e isso é dito.
    assert result.status == "ingested"
    assert result.row_count == 1
    assert "1 sem /votos" in result.detail
    assert session.execute(select(Vote)).scalars().all()[0].id_votacao == "BOA-1"


@respx.mock
def test_a_broken_source_still_raises(session):
    """Só 404 é tolerado. Um 500 significa fonte quebrada, não inconsistente — e
    engolir isso publicaria silêncio como se fosse ausência de dado."""
    _seed_camara_mandate(session)
    respx.get(url__regex=r"/votacoes\?").mock(
        return_value=httpx.Response(200, json=_votacoes_listing(["X-1"]))
    )
    respx.get(url__regex=r"/votacoes/[^/]+/orientacoes").mock(
        return_value=httpx.Response(200, json={"dados": []})
    )
    respx.get(url__regex=r"/votacoes/X-1/votos").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        VotacoesCollector().run(session, data_inicio="2026-02-01", data_fim="2026-02-28")


@respx.mock
def test_definitive_statuses_are_not_retried(session):
    """O cliente declarava um conjunto de status "retryable" e mesmo assim repetia
    todos. Um 404 custava 1+2+4 s de backoff por recurso inexistente."""
    route = respx.get(url__regex=r"/deputados/999$").mock(return_value=httpx.Response(404))

    with CamaraClient() as client, pytest.raises(httpx.HTTPStatusError):
        client.get("deputados/999")

    assert route.call_count == 1, "404 não deve ser repetido"


@respx.mock
def test_transient_statuses_are_still_retried(session):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"dados": []}) if calls["n"] > 1 else httpx.Response(503)

    respx.get(url__regex=r"/deputados/1$").mock(side_effect=handler)

    with CamaraClient() as client:
        client.get("deputados/1")

    assert calls["n"] == 2, "503 deve ser repetido até obter sucesso"
