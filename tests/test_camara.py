from __future__ import annotations

import httpx
import respx
from sqlalchemy import select
from tests.helpers import deputado_detail

from resumo.db.models import Mandate, Person
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.camara.deputados import DeputadosCollector

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
    assert mandate.sigla_uf == "SP"
    assert mandate.house_member_id == "1"
