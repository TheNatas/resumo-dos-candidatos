"""Senado collectors — every payload shape here mirrors a verified live response.

The fixtures deliberately reproduce the two traps this source is made of: the legacy
XML->JSON collapse of single-element arrays, and the secret-ballot roll-call where
every senator "voted" but no position is published.
"""

from __future__ import annotations

import httpx
import respx
from sqlalchemy import func, select

from resumo.db.models import AttendanceRecord, Expense, House, Mandate, Person, Proposition, Vote
from resumo.ingestion.senado.despesas import CEAPS_API_BASE, DespesasCollector
from resumo.ingestion.senado.proposicoes import ProposicoesCollector
from resumo.ingestion.senado.senadores import SenadoresCollector
from resumo.ingestion.senado.votacoes import VotacoesCollector, _year_windows

BASE = "https://legis.senado.leg.br/dadosabertos"


# ── legacy (/senador/*) payload builders — PascalCase, every value a string ──
def _ident(cod: str, nome: str, completo: str, uf: str = "SC", partido: str = "PT") -> dict:
    return {
        "CodigoParlamentar": cod,
        "NomeParlamentar": nome,
        "NomeCompletoParlamentar": completo,
        "SiglaPartidoParlamentar": partido,
        "UfParlamentar": uf,
    }


def _parlamentar(cod: str, nome: str, completo: str, uf: str = "SC") -> dict:
    return {
        "IdentificacaoParlamentar": _ident(cod, nome, completo, uf),
        # Single mandate -> the translator collapses the array into a bare object.
        "Mandatos": {
            "Mandato": {
                "CodigoMandato": f"9{cod}",
                "UfParlamentar": uf,
                "DescricaoParticipacao": "Titular",
                "PrimeiraLegislaturaDoMandato": {"NumeroLegislatura": "56"},
                "SegundaLegislaturaDoMandato": {"NumeroLegislatura": "57"},
            }
        },
    }


def _lista_legislatura(parlamentares: list[dict]) -> dict:
    # A one-senator result arrives as a bare object, not a one-element array.
    node = parlamentares[0] if len(parlamentares) == 1 else parlamentares
    return {"ListaParlamentarLegislatura": {"Parlamentares": {"Parlamentar": node}}}


def _lista_atual(parlamentares: list[dict]) -> dict:
    node = parlamentares[0] if len(parlamentares) == 1 else parlamentares
    return {"ListaParlamentarEmExercicio": {"Parlamentares": {"Parlamentar": node}}}


def _detalhe(cod: str, nome: str, completo: str, nascimento: str = "1965-03-22") -> dict:
    return {
        "DetalheParlamentar": {
            "Parlamentar": {
                "IdentificacaoParlamentar": _ident(cod, nome, completo),
                "DadosBasicosParlamentar": {
                    "DataNascimento": nascimento,
                    "Naturalidade": "Blumenau",
                    "UfNaturalidade": "SC",
                },
            }
        }
    }


def _mandatos(cod: str, inicio: str = "2023-02-01", fim: str | None = None) -> dict:
    exercicio = {"CodigoExercicio": f"1{cod}", "DataInicio": inicio}
    if fim:
        exercicio["DataFim"] = fim
    return {
        "MandatoParlamentar": {
            "Parlamentar": {
                "Mandatos": {
                    "Mandato": {
                        "CodigoMandato": f"9{cod}",
                        "PrimeiraLegislaturaDoMandato": {"NumeroLegislatura": "56"},
                        "SegundaLegislaturaDoMandato": {"NumeroLegislatura": "57"},
                        "Exercicios": {"Exercicio": exercicio},
                    }
                }
            }
        }
    }


def _mock_roster(listed: list[dict], atual: list[dict] | None = None) -> None:
    respx.get(url__regex=r"/senador/lista/legislatura/57").mock(
        return_value=httpx.Response(200, json=_lista_legislatura(listed))
    )
    respx.get(url__regex=r"/senador/lista/atual").mock(
        return_value=httpx.Response(200, json=_lista_atual(atual if atual is not None else listed))
    )

    def detail(request):
        cod = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_detalhe(cod, f"SENADOR {cod}", f"NOME CIVIL {cod}"))

    def mandatos(request):
        cod = request.url.path.split("/")[-2]
        return httpx.Response(200, json=_mandatos(cod))

    respx.get(url__regex=r"/senador/\d+/mandatos$").mock(side_effect=mandatos)
    respx.get(url__regex=r"/senador/\d+$").mock(side_effect=detail)


# ── modern (/votacao) payload builders — camelCase, native types ─────────────
def _voto(cod: str, sigla: str, descricao: str, uf: str = "SC") -> dict:
    return {
        "codigoParlamentar": int(cod),
        "nomeParlamentar": f"SENADOR {cod}",
        "siglaPartidoParlamentar": "PT",
        "siglaUFParlamentar": uf,
        "siglaVotoParlamentar": sigla,
        "descricaoVotoParlamentar": descricao,
    }


def _votacao(votos: list[dict], *, secreta: bool = False, sessao: int = 5551) -> dict:
    return {
        "codigoSessao": sessao,
        "dataSessao": "2024-03-14",
        "idProcesso": 8123456,
        "codigoMateria": 155000,
        "identificacao": "PL 1234/2023",
        "sigla": "PL",
        "numero": "1234",
        "ano": 2023,
        "ementa": "Dispõe sobre coisa nenhuma.",
        "codigoSessaoVotacao": sessao * 10,
        "sequencialVotacao": 1,
        "votacaoSecreta": "S" if secreta else "N",
        "descricaoVotacao": "Projeto de Lei",
        "resultadoVotacao": "Aprovado",
        # Aggregates exist only on secret votações; nominal ones publish null.
        "totalVotosSim": 44 if secreta else None,
        "totalVotosNao": 21 if secreta else None,
        "totalVotosAbstencao": 0 if secreta else None,
        "votos": votos,
    }


def _seed_mandate(session, member_id: str, leg: int = 57) -> Mandate:
    mandate = Mandate(
        house=House.SENADO,
        house_member_id=member_id,
        id_legislatura=leg,
        nome_parlamentar=f"SENADOR {member_id}",
        sigla_uf="SC",
    )
    session.add(mandate)
    session.flush()
    return mandate


# ── senadores ────────────────────────────────────────────────────────────────
@respx.mock
def test_senadores_collector_seeds_person_and_mandate_without_cpf(session):
    # The legislatura list is UF-filtered server-side, but a BA senator is included
    # here to prove the client-side filter is the one we actually rely on.
    _mock_roster([_parlamentar("5012", "FULANO", "FULANO DE TAL"),
                  _parlamentar("4981", "BELTRANO", "BELTRANO DA BAHIA", uf="BA")])

    res = SenadoresCollector().run(session, id_legislatura=57)
    session.commit()

    assert res.row_count == 1
    mandate = session.scalar(select(Mandate))
    assert mandate.house is House.SENADO
    assert mandate.house_member_id == "5012"
    assert mandate.sigla_uf == "SC"
    assert mandate.condicao_eleitoral == "Titular"
    assert mandate.situacao == "Exercício"
    assert mandate.data_inicio.isoformat() == "2023-02-01"
    assert mandate.data_fim is None  # exercício still open

    person = session.get(Person, mandate.person_id)
    # 🚨 Contract: the Senado publishes NO CPF, so this MUST stay None — the whole
    # reason senator resolution is probabilistic rather than deterministic.
    assert person.cpf is None
    assert person.nome_civil == "NOME CIVIL 5012"
    assert person.nome_normalizado == "NOME CIVIL 5012"
    assert person.data_nascimento.isoformat() == "1965-03-22"
    assert person.uf_nascimento == "SC"


@respx.mock
def test_single_element_legacy_response_is_normalized(session):
    """One senator -> `Parlamentar` is a dict, not a list (and so is Mandato/Exercicio)."""
    payload = _lista_legislatura([_parlamentar("5012", "FULANO", "FULANO DE TAL")])
    assert isinstance(payload["ListaParlamentarLegislatura"]["Parlamentares"]["Parlamentar"], dict)

    _mock_roster([_parlamentar("5012", "FULANO", "FULANO DE TAL")])
    res = SenadoresCollector().run(session, id_legislatura=57)
    session.commit()

    assert res.row_count == 1
    assert session.scalar(select(func.count()).select_from(Mandate)) == 1


@respx.mock
def test_senadores_collector_is_idempotent(session):
    _mock_roster([_parlamentar("5012", "FULANO", "FULANO DE TAL")])
    SenadoresCollector().run(session, id_legislatura=57)
    SenadoresCollector().run(session, id_legislatura=57)
    session.commit()

    assert session.scalar(select(func.count()).select_from(Mandate)) == 1
    assert session.scalar(select(func.count()).select_from(Person)) == 1


# ── votações ─────────────────────────────────────────────────────────────────
def test_year_windows_respect_the_one_year_cap():
    assert _year_windows("2023-02-01", "2023-12-31") == [("2023-02-01", "2023-12-31")]
    assert _year_windows("2024-06-01", "2026-02-10") == [
        ("2024-06-01", "2024-12-31"),
        ("2025-01-01", "2025-12-31"),
        ("2026-01-01", "2026-02-10"),
    ]


@respx.mock
def test_secret_votacao_stores_no_vote_positions(session):
    _seed_mandate(session, "5012")
    _seed_mandate(session, "5013")
    respx.get(url__regex=r"/votacao").mock(
        return_value=httpx.Response(
            200,
            json=[
                _votacao(
                    [_voto("5012", "Votou", "Votou"), _voto("5013", "Votou", "Votou")],
                    secreta=True,
                )
            ],
        )
    )

    res = VotacoesCollector().run(session, data_inicio="2024-01-01", data_fim="2024-12-31")
    session.commit()

    assert res.row_count == 0
    assert session.scalar(select(func.count()).select_from(Vote)) == 0
    # ...but taking part in a secret ballot is still evidence of presence.
    presencas = session.scalars(select(AttendanceRecord)).all()
    assert len(presencas) == 2
    assert all(p.presente is True for p in presencas)
    assert all(p.derivation == "senado_votacao_comparecimento" for p in presencas)


@respx.mock
def test_attendance_codes_are_not_stored_as_vote_positions(session):
    _seed_mandate(session, "5012")
    _seed_mandate(session, "5013")
    _seed_mandate(session, "5014")
    respx.get(url__regex=r"/votacao").mock(
        return_value=httpx.Response(
            200,
            json=[
                _votacao(
                    [
                        _voto("5012", "Sim", "Sim"),
                        _voto("5013", "NCom", "Não Compareceu"),
                        _voto("5014", "AP", "Ausência Justificada"),
                    ]
                )
            ],
        )
    )

    VotacoesCollector().run(session, data_inicio="2024-01-01", data_fim="2024-12-31")
    session.commit()

    votes = {v.house_member_id: v for v in session.scalars(select(Vote)).all()}
    assert set(votes) == {"5012"}  # NCom/AP are attendance markers, never positions
    assert votes["5012"].tipo_voto == "Sim"
    assert votes["5012"].id_proposicao == "SF8123456"
    assert votes["5012"].id_votacao == "SF55510"
    assert votes["5012"].data_votacao.isoformat() == "2024-03-14"
    # The Senado publishes no party orientation anywhere.
    assert votes["5012"].orientacao_partido is None

    presencas = {a.house_member_id: a for a in session.scalars(select(AttendanceRecord)).all()}
    assert presencas["5012"].presente is True
    assert presencas["5013"].presente is False
    assert presencas["5013"].justificativa is None
    assert presencas["5014"].presente is False
    assert presencas["5014"].justificativa == "Ausência Justificada"
    assert presencas["5013"].id_evento == "SF5551"  # the session, not the votação


# ── proposições ──────────────────────────────────────────────────────────────
@respx.mock
def test_proposicoes_are_prefixed_and_keep_situacao(session):
    mandate = _seed_mandate(session, "5012")
    respx.get(url__regex=r"/processo").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 8123456,
                    "codigoMateria": 155000,
                    "identificacao": "PL 1234/2023",
                    "sigla": "PL",
                    "numero": "1234",
                    "ano": 2023,
                    "autoria": "Senador Fulano de Tal (PT/SC), Senadora Cicrana (PP/BA)",
                    "ementa": "Dispõe sobre coisa nenhuma.",
                    "dataApresentacao": "2023-04-11",
                    "situacaoAtual": "Aguardando designação do relator",
                    "tramitando": "S",
                }
            ],
        )
    )

    ProposicoesCollector().run(session, id_legislatura=57)
    session.commit()

    prop = session.scalar(select(Proposition))
    # "SF" keeps Senado ids from colliding with Câmara ids in the shared PK.
    assert prop.proposition_id == "SF8123456"
    assert prop.house is House.SENADO
    assert prop.authoring_mandate_id == mandate.id
    assert prop.situacao == "Aguardando designação do relator"
    assert prop.numero == 1234


# ── CEAPS ────────────────────────────────────────────────────────────────────
def _ceaps_row(cod: int, doc_id: int, valor: float = 1234.56) -> dict:
    return {
        "id": doc_id,
        "tipoDocumento": "Nota Fiscal",
        "ano": 2024,
        "mes": 3,
        "codSenador": cod,
        "nomeSenador": f"SENADOR {cod}",
        "tipoDespesa": "Passagens aéreas, aquáticas e terrestres nacionais",
        "cpfCnpj": "12345678000199",
        "fornecedor": "CIA AEREA SA",
        "documento": f"NF-{doc_id}",
        "data": "2024-03-14",
        "detalhamento": "Trecho FLN/BSB",
        "valorReembolsado": valor,
    }


@respx.mock
def test_ceaps_rows_upsert_idempotently_and_ignore_unknown_senators(session):
    mandate = _seed_mandate(session, "5012")
    respx.get(url__regex=rf"{CEAPS_API_BASE}/api/v1/senadores/despesas_ceaps/2024").mock(
        return_value=httpx.Response(
            200,
            json=[
                _ceaps_row(5012, 7001),
                _ceaps_row(5012, 7002, valor=98.7),
                _ceaps_row(9999, 7003),  # senator outside the scoped roster
            ],
        )
    )

    first = DespesasCollector().run(session, anos=[2024], id_legislatura=57)
    session.commit()
    assert first.row_count == 2

    expenses = session.scalars(select(Expense)).all()
    assert len(expenses) == 2
    row = next(e for e in expenses if e.cod_documento == "7001")
    assert row.house is House.SENADO
    assert row.mandate_id == mandate.id
    assert float(row.valor_liquido) == 1234.56
    assert row.num_documento == "NF-7001"
    # cpfCnpj is the SUPPLIER's document, never the senator's.
    assert row.cnpj_cpf_fornecedor == "12345678000199"
    assert row.nome_fornecedor == "CIA AEREA SA"

    DespesasCollector().run(session, anos=[2024], id_legislatura=57)
    session.commit()
    assert session.scalar(select(func.count()).select_from(Expense)) == 2
