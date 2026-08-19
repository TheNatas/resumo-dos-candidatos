"""Ponte de CPF pelo histórico do TSE.

A ALESC não publica CPF nem nascimento, então o vínculo dependia de comparar nomes —
e comparar nomes emparelhava pessoas diferentes (SALMIR DA SILVA com o mandato de
ALTAIR DA SILVA). A ponte troca o palpite por um documento.
"""

from __future__ import annotations

import pytest

from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    ConfidenceTier,
    House,
    Mandate,
    MatchMethod,
    Person,
    ReviewQueue,
)
from resumo.resolution.bridge import recover_cpfs
from resumo.resolution.pipeline import resolve

# CPFs válidos de verdade: `valid_cpf` recusa dígitos repetidos, então
# "11111111111" nunca chegaria ao comparador.
CPF_TITULAR = "12345678909"
CPF_OUTRO = "52998224725"


def _mandato(session, *, nome_parlamentar="Altair Silva", uf="SC"):
    person = Person(nome_normalizado=nome_parlamentar.upper(), nome_civil=nome_parlamentar)
    session.add(person)
    session.flush()
    mandate = Mandate(
        house=House.ASSEMBLEIA, house_member_id="alt-silva", id_legislatura=20,
        person_id=person.id, sigla_uf=uf, nome_parlamentar=nome_parlamentar, data_fim=None,
    )
    session.add(mandate)
    session.flush()
    return mandate


def _passada(session, *, nome_urna, nome="ALTAIR DA SILVA", cpf=CPF_TITULAR,
             situacao="ELEITO POR QP", ano=2022, sq="P1", uf="SC"):
    session.add(
        Candidacy(
            sq_candidato=sq, ano_eleicao=ano, sg_uf=uf, cd_cargo=7,
            ds_cargo="DEPUTADO ESTADUAL", nome_candidato=nome, nome_urna=nome_urna,
            nome_normalizado=nome, cpf_raw=cpf, sg_partido="PT",
            ds_sit_tot_turno=situacao,
        )
    )
    session.commit()


def test_recovers_cpf_from_a_unique_ballot_name(session):
    mandate = _mandato(session)
    _passada(session, nome_urna="Altair Silva")

    bridged = recover_cpfs(session, before_year=2026)

    ident = bridged[str(mandate.id)]
    assert ident.cpf == CPF_TITULAR
    assert ident.source_year == 2022
    assert ident.source_nome == "ALTAIR DA SILVA"


def test_ambiguous_ballot_name_recovers_nothing(session):
    """Duas pessoas com o mesmo nome de urna é exatamente o caso em que um palpite
    atribuiria o histórico de uma à outra. Ambiguidade não se desempata."""
    mandate = _mandato(session)
    _passada(session, nome_urna="Altair Silva", sq="P1", cpf=CPF_TITULAR)
    _passada(session, nome_urna="Altair Silva", sq="P2", cpf=CPF_OUTRO, nome="ALTAIR SOUZA")

    assert str(mandate.id) not in recover_cpfs(session, before_year=2026)


def test_the_same_person_across_elections_is_not_ambiguity(session):
    mandate = _mandato(session)
    _passada(session, nome_urna="Altair Silva", sq="P1", ano=2018)
    _passada(session, nome_urna="Altair Silva", sq="P2", ano=2022)

    ident = recover_cpfs(session, before_year=2026)[str(mandate.id)]
    assert ident.cpf == CPF_TITULAR
    assert ident.source_year == 2022  # a mais recente


def test_a_defeated_candidate_is_not_a_bridge(session):
    """Quem perdeu não assume mandato; aceitar essa linha abriria a ponte para um
    homônimo derrotado."""
    mandate = _mandato(session)
    _passada(session, nome_urna="Altair Silva", situacao="NÃO ELEITO")

    assert str(mandate.id) not in recover_cpfs(session, before_year=2026)


def test_only_past_elections_count(session):
    mandate = _mandato(session)
    _passada(session, nome_urna="Altair Silva", ano=2026)

    assert str(mandate.id) not in recover_cpfs(session, before_year=2026)


@pytest.fixture
def _bridged_world(session):
    """Um mandato da ALESC cuja identidade é recuperável, e duas candidaturas 2026:
    a da mesma pessoa e a de um homônimo parcial."""
    mandate = _mandato(session)
    _passada(session, nome_urna="Altair Silva")
    session.add_all(
        [
            Candidacy(
                sq_candidato="C-TITULAR", ano_eleicao=2026, sg_uf="SC", cd_cargo=7,
                ds_cargo="DEPUTADO ESTADUAL", nome_candidato="ALTAIR DA SILVA",
                nome_urna="ALTAIR SILVA", nome_normalizado="ALTAIR DA SILVA",
                cpf_raw=CPF_TITULAR, sg_partido="PT",
            ),
            Candidacy(
                sq_candidato="C-HOMONIMO", ano_eleicao=2026, sg_uf="SC", cd_cargo=7,
                ds_cargo="DEPUTADO ESTADUAL", nome_candidato="SALMIR DA SILVA",
                nome_urna="SALMIR DA SILVA", nome_normalizado="SALMIR DA SILVA",
                cpf_raw=CPF_OUTRO, sg_partido="PP",
            ),
        ]
    )
    session.commit()
    return mandate


def test_bridged_cpf_publishes_the_link_as_auto_strong(session, _bridged_world):
    result = resolve(session, year=2026)
    session.commit()

    assert result.via_tse == 1
    link = session.query(CandidateMandateLink).filter_by(sq_candidato="C-TITULAR").one()
    assert link.match_method is MatchMethod.cpf_via_tse
    assert link.confidence_tier is ConfidenceTier.auto_strong
    # Método próprio, não `cpf_exact`: o vínculo é igualdade de CPF, mas o CPF veio
    # por um salto de nome, e a ficha não deve anunciar mais certeza do que existe.
    assert link.match_method is not MatchMethod.cpf_exact


def test_a_closed_mandate_is_never_proposed_for_another_cpf(session, _bridged_world):
    """O par que a comparação de nomes proporia (SALMIR ↔ Altair Silva) está morto:
    o documento já disse que não é a mesma pessoa."""
    resolve(session, year=2026)
    session.commit()

    assert session.query(CandidateMandateLink).filter_by(sq_candidato="C-HOMONIMO").count() == 0
    assert session.query(ReviewQueue).filter_by(sq_candidato="C-HOMONIMO").count() == 0


def test_without_a_bridge_nothing_changes(session):
    """Mandato sem ponte continua sujeito às regras antigas — a ponte só acrescenta
    certeza onde existe documento, nunca remove o caminho anterior."""
    _mandato(session)  # nenhuma candidatura passada correspondente
    session.add(
        Candidacy(
            sq_candidato="C1", ano_eleicao=2026, sg_uf="SC", cd_cargo=7,
            ds_cargo="DEPUTADO ESTADUAL", nome_candidato="ALTAIR DA SILVA",
            nome_urna="ALTAIR SILVA", nome_normalizado="ALTAIR DA SILVA",
            cpf_raw=CPF_TITULAR, sg_partido="PT",
        )
    )
    session.commit()

    result = resolve(session, year=2026)

    assert result.via_tse == 0
    # Sem CPF do lado do mandato, o par volta a depender do comparador de nomes.
    assert result.links + result.review >= 1


def test_connectives_and_stray_initials_do_not_break_the_bridge(session):
    """A urna registra "GERRI DA CONSOLI" e "A ALEX BRASIL" onde a Casa escreve
    "Gerri Consoli" e "Alex Brasil". Comparar como texto perderia a ponte por um
    "DA" — e devolveria a decisão para um humano, que é o que se quer evitar."""
    mandate = _mandato(session, nome_parlamentar="Alex Brasil")
    _passada(session, nome_urna="A ALEX BRASIL", nome="ALEXANDER BRASIL ALVES PEREIRA")

    ident = recover_cpfs(session, before_year=2026)[str(mandate.id)]
    assert ident.cpf == CPF_TITULAR


def test_house_may_write_the_name_more_fully_than_the_ballot(session):
    """"PADRE PEDRO" na urna vira "Padre Pedro Baldissera" na Casa. Só vale porque
    "BALDISSERA" aparece no nome civil da mesma candidatura."""
    mandate = _mandato(session, nome_parlamentar="Padre Pedro Baldissera")
    _passada(session, nome_urna="PADRE PEDRO", nome="PEDRO BALDISSERA")

    ident = recover_cpfs(session, before_year=2026)[str(mandate.id)]
    assert ident.cpf == CPF_TITULAR
    assert "nome civil" in ident.matched_on


def test_the_extra_surname_must_be_corroborated_by_the_civil_name(session):
    """Sem essa exigência, qualquer sobrenome serviria: "PEDRO" casaria com
    "Pedro Qualquer Coisa" e o histórico iria para a ficha errada."""
    mandate = _mandato(session, nome_parlamentar="Pedro Silvestre")
    # O nome civil não contém "SILVESTRE".
    _passada(session, nome_urna="PEDRO", nome="PEDRO DE ASSIS SOUZA")

    assert str(mandate.id) not in recover_cpfs(session, before_year=2026)


def test_two_people_matching_the_looser_rule_is_still_ambiguity(session):
    mandate = _mandato(session, nome_parlamentar="Pedro Silvestre")
    _passada(session, nome_urna="PEDRO", nome="PEDRO DE ASSIS SILVESTRE",
             sq="P1", cpf=CPF_TITULAR)
    _passada(session, nome_urna="PEDRO", nome="PEDRO SILVESTRE JUNIOR",
             sq="P2", cpf=CPF_OUTRO)

    assert str(mandate.id) not in recover_cpfs(session, before_year=2026)
