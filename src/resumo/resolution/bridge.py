"""Recuperação de CPF pelo histórico do próprio TSE.

Uma Casa que não publica CPF nem data de nascimento (a ALESC) deixa o nome como
única ponte — e nome sozinho nunca é evidência suficiente para publicar o histórico
de alguém na ficha de outro. Mas quem exerce um mandato estadual **se elegeu**, e
quem se elegeu está no arquivo do TSE daquela eleição, com CPF.

Então a identificação não precisa depender de nome nenhum na ponta que importa:

    candidatura 2026  ──CPF (idêntico)──▶  candidatura 2022 (TSE)
                                                 │
                                 nome de urna ≡ nome parlamentar
                                                 ▼
                                           mandato ALESC

O salto por nome continua existindo, mas em condições muito mais estreitas que a
comparação de nomes civis que ele substitui:

- compara **nome de urna com nome parlamentar** — a mesma convenção de nomeação,
  não um nome civil contra um apelido;
- exige igualdade exata do nome normalizado, não similaridade;
- exige que a correspondência seja **única** dentro do conjunto;
- o conjunto é minúsculo e fechado: mesmo cargo, mesma UF, mesma eleição;
- e o registro tem que ser de quem **ocupou ou podia ocupar a cadeira**
  (eleito ou suplente) — quem perdeu a eleição não vira titular de mandato.

O que a ponte devolve é usado nos dois sentidos, e o negativo é o mais valioso:
um mandato com CPF conhecido está **fechado** para qualquer outro CPF, o que refuta
de uma vez todos os pares que o comparador de nomes propôs por acaso.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo import cargos
from resumo.db.models import Candidacy, House, Mandate
from resumo.util import normalize_name, valid_cpf

# Situações que significam "ocupou ou podia ocupar a cadeira": "ELEITO",
# "ELEITO POR QP", "ELEITO POR MÉDIA", "SUPLENTE". Quem ficou como não eleito não
# assume mandato, e aceitar essa linha abriria a ponte para um homônimo derrotado.
# Comparação por prefixo, não por substring: "NÃO ELEITO" contém "ELEITO".
_SEATED_PREFIXES = ("ELEITO", "SUPLENTE")

# Conectivos e iniciais soltas não distinguem pessoas: a urna registra
# "GERRI DA CONSOLI" e "A ALEX BRASIL" onde a Casa escreve "Gerri Consoli" e
# "Alex Brasil". Compará-los como texto faria a ponte falhar por causa de um "DA".
_CONECTIVOS = frozenset({"DA", "DE", "DO", "DAS", "DOS", "E"})


def _tokens(nome: str | None) -> frozenset[str]:
    return frozenset(
        t
        for t in (normalize_name(nome) or "").split()
        if len(t) > 1 and t not in _CONECTIVOS
    )


@dataclass(frozen=True)
class BridgedIdentity:
    """CPF recuperado para um mandato, com a proveniência que o justifica."""

    cpf: str
    source_year: int
    source_sq_candidato: str
    source_nome: str
    matched_on: str  # o nome de urna normalizado que fechou a ponte


def _seated(situacao: str | None) -> bool:
    s = (situacao or "").upper().strip()
    return s.startswith(_SEATED_PREFIXES)


def recover_cpfs(
    session: Session, *, before_year: int, houses: tuple[House, ...] = (House.ASSEMBLEIA,)
) -> dict[str, BridgedIdentity]:
    """CPF por mandato (chave: `str(mandate.id)`), recuperado de eleições anteriores.

    Só devolve entrada quando a correspondência é exata e única. Ambiguidade não é
    resolvida por desempate: duas pessoas com o mesmo nome de urna são exatamente o
    caso em que um palpite atribuiria o histórico de uma à outra.
    """
    mandates = list(
        session.execute(select(Mandate).where(Mandate.house.in_(houses))).scalars()
    )
    if not mandates:
        return {}

    cargo_codes = [c for c, h in cargos.CARGO_HOUSE.items() if h in houses]
    ufs = {m.sigla_uf for m in mandates if m.sigla_uf}

    stmt = select(Candidacy).where(
        Candidacy.ano_eleicao < before_year,
        Candidacy.cd_cargo.in_(cargo_codes),
        Candidacy.cpf_raw.is_not(None),
    )
    if ufs:
        stmt = stmt.where(Candidacy.sg_uf.in_(ufs))

    # Candidaturas passadas que ocuparam cadeira, agrupadas por UF.
    por_uf: dict[str, list[Candidacy]] = defaultdict(list)
    for cand in session.execute(stmt).scalars():
        if not _seated(cand.ds_sit_tot_turno):
            continue
        # Mesma normalização que o resolvedor aplica do outro lado da comparação:
        # ele indexa por `valid_cpf(cpf_raw)`, que descarta máscara e sentinela do
        # TSE. Comparar cru contra validado nunca casaria — ou pior, casaria por
        # engano em valores mascarados iguais.
        if not valid_cpf(cand.cpf_raw):
            continue
        if _tokens(cand.nome_urna):
            por_uf[cand.sg_uf or ""].append(cand)

    bridged: dict[str, BridgedIdentity] = {}
    for mandate in mandates:
        alvo = _tokens(mandate.nome_parlamentar)
        if not alvo:
            continue
        pool = por_uf.get(mandate.sigla_uf or "", [])

        # Nível 1 — o mesmo nome, ignorando conectivo e inicial solta.
        hits = [c for c in pool if _tokens(c.nome_urna) == alvo]
        regra = "urna == nome parlamentar"

        # Nível 2 — a Casa escreve o nome mais completo que a urna: "PADRE PEDRO"
        # vira "Padre Pedro Baldissera", "PEDRÃO" vira "Pedrão Silvestre". Só vale
        # quando o que a Casa acrescentou aparece no NOME CIVIL daquela mesma
        # candidatura: sem essa corroboração, qualquer sobrenome serviria.
        if not hits:
            hits = [
                c
                for c in pool
                if _tokens(c.nome_urna) <= alvo
                and (alvo - _tokens(c.nome_urna)) <= _tokens(c.nome_candidato)
            ]
            regra = "urna ⊂ nome parlamentar, resto confirmado pelo nome civil"

        # Uma mesma pessoa em mais de uma eleição não é ambiguidade; duas pessoas
        # diferentes, sim — e é exatamente o caso em que um desempate atribuiria o
        # histórico de uma à outra.
        cpfs = {valid_cpf(c.cpf_raw) for c in hits}
        if len(cpfs) != 1:
            continue
        mais_recente = max(hits, key=lambda c: c.ano_eleicao or 0)
        bridged[str(mandate.id)] = BridgedIdentity(
            cpf=valid_cpf(mais_recente.cpf_raw),
            source_year=mais_recente.ano_eleicao,
            source_sq_candidato=mais_recente.sq_candidato,
            source_nome=mais_recente.nome_candidato or "",
            matched_on=f"{normalize_name(mais_recente.nome_urna)} ({regra})",
        )
    return bridged
