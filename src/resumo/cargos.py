"""Cargo taxonomy — TSE office codes and what each office implies.

Two distinctions the platform depends on and that are easy to conflate:

1. **Sistema eleitoral** (majoritário vs proporcional) — an electoral-math property.
   Senador is *majoritário* but files no proposta de governo.
2. **Proposta de governo obrigatória** — only the *executive* majoritarian offices
   (Presidente, Governador, Prefeito) must file one (Lei 9.504/97, art. 11 §1º XI).

So `is_majoritario` and "has a proposta" are NOT the same predicate; keeping them
apart is what lets the front say the right thing for a senador.

3. **Fonte de histórico** — whether a track record can exist *at all* for the office,
   and from which body. This drives the honest message on the ficha:
   "histórico disponível" vs "cobertura parcial" vs "não há fonte pública" vs
   "não se aplica a cargo executivo".

   🚨 "Cargo executivo" and "não se aplica" stopped being synonyms once the executive
   collectors landed. GOVERNADOR now has a real, collected record (the acts sent to
   the Assembly) and is `partial`; the remaining executive offices keep
   `not_applicable` because nothing reads them, not because the office forbids it.
"""

from __future__ import annotations

import datetime as dt
import enum

from resumo.db.models import House

# ── TSE CD_CARGO ─────────────────────────────────────────────────────────────
PRESIDENTE = 1
VICE_PRESIDENTE = 2
GOVERNADOR = 3
VICE_GOVERNADOR = 4
SENADOR = 5
DEPUTADO_FEDERAL = 6
DEPUTADO_ESTADUAL = 7
DEPUTADO_DISTRITAL = 8
PRIMEIRO_SUPLENTE = 9
SEGUNDO_SUPLENTE = 10
PREFEITO = 11
VICE_PREFEITO = 12
VEREADOR = 13

CARGO_NAMES: dict[int, str] = {
    PRESIDENTE: "PRESIDENTE",
    VICE_PRESIDENTE: "VICE-PRESIDENTE",
    GOVERNADOR: "GOVERNADOR",
    VICE_GOVERNADOR: "VICE-GOVERNADOR",
    SENADOR: "SENADOR",
    DEPUTADO_FEDERAL: "DEPUTADO FEDERAL",
    DEPUTADO_ESTADUAL: "DEPUTADO ESTADUAL",
    DEPUTADO_DISTRITAL: "DEPUTADO DISTRITAL",
    PRIMEIRO_SUPLENTE: "1º SUPLENTE",
    SEGUNDO_SUPLENTE: "2º SUPLENTE",
    PREFEITO: "PREFEITO",
    VICE_PREFEITO: "VICE-PREFEITO",
    VEREADOR: "VEREADOR",
}

# Elected by majority (as opposed to the proportional/quotient system). Suplentes de
# senador ride on the senator's ticket, so they are majoritarian too.
MAJORITARIAN_CARGOS: frozenset[int] = frozenset(
    {
        PRESIDENTE,
        VICE_PRESIDENTE,
        GOVERNADOR,
        VICE_GOVERNADOR,
        SENADOR,
        PRIMEIRO_SUPLENTE,
        SEGUNDO_SUPLENTE,
        PREFEITO,
        VICE_PREFEITO,
    }
)

# Must file a proposta de governo. Executive offices only — NOT senador.
PROPOSTA_CARGOS: frozenset[int] = frozenset({PRESIDENTE, GOVERNADOR, PREFEITO})

# The four offices on the ballot in a state during a general election year.
ESTADUAL_GENERAL_CARGOS: frozenset[int] = frozenset(
    {GOVERNADOR, SENADOR, DEPUTADO_FEDERAL, DEPUTADO_ESTADUAL}
)


class HistoryAvailability(str, enum.Enum):
    """Why a candidacy does or does not get a track-record section."""

    available = "available"
    """A public source exists and is collected; absence of data means the person is
    not an incumbent (or the link is unconfirmed)."""

    partial = "partial"
    """A source exists and is collected, but it is materially thinner than the
    federal equivalent, in a way a reader would be misled by if we did not say so.
    Comparisons ACROSS houses are invalid at this level and must be discouraged."""

    no_public_source = "no_public_source"
    """The office has a legislative record, but no machine-readable public source is
    collected for it. Absence of data says nothing about the candidate."""

    not_applicable = "not_applicable"
    """An executive office: there is no roll-call/attendance record by nature."""


# Which body holds the track record for an office, if any.
#
# This map does double duty, and the second job is easy to miss: it is also what
# decides whether a confirmed mandate is a *re-election bid*. `queries` compares
# `house_for(cd_cargo)` against the house of the accepted link, so an office missing
# from here can never be flagged "tentando reeleição" no matter what mandate its
# holder demonstrably has. GOVERNADOR was absent until the executive collectors
# landed, which is why a sitting governor seeking a second term read as a challenger.
CARGO_HOUSE: dict[int, House] = {
    DEPUTADO_FEDERAL: House.CAMARA,
    SENADOR: House.SENADO,
    DEPUTADO_ESTADUAL: House.ASSEMBLEIA,
    GOVERNADOR: House.EXECUTIVO,
}

_HISTORY: dict[int, HistoryAvailability] = {
    PRESIDENTE: HistoryAvailability.not_applicable,
    VICE_PRESIDENTE: HistoryAvailability.not_applicable,
    # 🚨 `partial`, and deliberately not `available`. A governor's record here is one
    # slice of the office — the acts they signed *towards the Assembly*: bills of
    # executive initiative and vetoes. That is a real, substantive record, and it is
    # emphatically not the whole of governing (budget execution, decrees, appointments
    # and programmes are all outside it). `partial` is the enum member that makes the
    # ficha print the caveat saying so, which is the only honest way to show a number
    # that covers part of a job. See HISTORY_NOTE[GOVERNADOR].
    #
    # The other executive offices stay `not_applicable`: no collector reads them, and
    # claiming otherwise would promise a section that renders empty.
    GOVERNADOR: HistoryAvailability.partial,
    VICE_GOVERNADOR: HistoryAvailability.not_applicable,
    PREFEITO: HistoryAvailability.not_applicable,
    VICE_PREFEITO: HistoryAvailability.not_applicable,
    DEPUTADO_FEDERAL: HistoryAvailability.available,
    SENADOR: HistoryAvailability.available,
    # Suplente de senador holds no seat unless called up; treated as no source.
    PRIMEIRO_SUPLENTE: HistoryAvailability.no_public_source,
    SEGUNDO_SUPLENTE: HistoryAvailability.no_public_source,
    # ALESC publishes no API, but e-Legis is scrapable and the expense CSVs are
    # excellent. The catch is roll-calls: ~96% of ALESC matters are decided by
    # votação simbólica, which records no individual position at all. See
    # HISTORY_NOTE — this is a property of the house, not a publishing gap.
    DEPUTADO_ESTADUAL: HistoryAvailability.partial,
    DEPUTADO_DISTRITAL: HistoryAvailability.no_public_source,
    VEREADOR: HistoryAvailability.no_public_source,
}

# Human-readable reason shown on the ficha when history is absent. Keyed by cargo so
# the copy is specific ("assembleias estaduais não publicam...") rather than generic.
HISTORY_NOTE: dict[int, str] = {
    GOVERNADOR: (
        "O histórico de um cargo executivo não é comparável ao de um parlamentar: não "
        "há votação nominal, presença em plenário nem cota de gabinete a exibir. O que "
        "aparece aqui são os atos que o governador assinou perante a Assembleia — "
        "projetos de lei de iniciativa do Executivo e mensagens de veto (total ou "
        "parcial) —, coletados do e-Legis da ALESC. É uma fatia real do mandato, mas "
        "apenas uma: execução orçamentária, decretos, nomeações e programas de governo "
        "ficam fora, e a ausência de um número aqui não diz nada sobre eles."
    ),
    DEPUTADO_ESTADUAL: (
        "Gastos de gabinete, proposições e presença da ALESC são completos; já as "
        "votações são limitadas por uma característica da própria "
        "Casa: cerca de 96% das matérias são decididas em votação simbólica, que "
        "por definição não registra a posição individual de cada deputado. Só há "
        "voto nominal registrado nas matérias controversas (~200 no total), e o "
        "portal e-Legis não tem dados anteriores a fevereiro de 2023. Números de "
        "votação NÃO são comparáveis com os de deputados federais."
    ),
    DEPUTADO_DISTRITAL: (
        "Câmara Legislativa do DF fora do escopo atual (plataforma restrita a SC)."
    ),
    VEREADOR: "Câmaras municipais fora do escopo (eleição geral, não municipal).",
    PRIMEIRO_SUPLENTE: "Suplente de senador não exerce mandato salvo convocação.",
    SEGUNDO_SUPLENTE: "Suplente de senador não exerce mandato salvo convocação.",
}

# Caveat about the RECORD ITSELF, keyed by the house that produced it — distinct
# from HISTORY_NOTE, which is about the office being sought. Both can apply at once:
# a deputado estadual candidate gets the cargo note, and whichever house their
# confirmed mandate sits in supplies this one.
#
# It exists because vote counts are NOT comparable across houses, and a reader
# looking at two numbers side by side will assume they are.
HOUSE_CAVEAT: dict[House, str] = {
    House.SENADO: (
        "No Senado, cerca de 57% das votações da legislatura são secretas — nelas a "
        "posição individual não é publicada, apenas os totais. O número de votos "
        "nominais abaixo cobre só as votações abertas e é naturalmente muito menor "
        "que o de um deputado federal."
    ),
    House.ASSEMBLEIA: (
        "Na ALESC cerca de 96% das matérias são decididas em votação simbólica, que "
        "não registra a posição individual. Só há voto nominal nas matérias "
        "controversas, e não há registro anterior a fevereiro de 2023."
    ),
    House.EXECUTIVO: (
        "Este é um mandato executivo, e os contadores de um parlamentar não existem "
        "nele: um governador não vota em plenário, não tem presença registrada e não "
        "usa cota de gabinete — a ausência desses números é uma característica do "
        "cargo, não uma falha de coleta. O que a Assembleia publica do Executivo são "
        "os atos que ele lhe envia: projetos de iniciativa do governador e mensagens "
        "de veto. Não compare esses totais com os de um deputado: medem coisas "
        "diferentes."
    ),
}


def house_caveat(house: House | None) -> str | None:
    """Caveat about the completeness of a house's published record, or None."""
    return HOUSE_CAVEAT.get(house) if house is not None else None


_EXECUTIVE_NOTE = (
    "Cargo executivo: não há votações nominais, proposições ou registro de presença "
    "a exibir. A prestação de contas do mandato executivo é outra fonte, fora do "
    "escopo desta plataforma."
)


def is_majoritario(cd_cargo: int | None) -> bool:
    return cd_cargo in MAJORITARIAN_CARGOS


def requires_proposta(cd_cargo: int | None) -> bool:
    return cd_cargo in PROPOSTA_CARGOS


def history_availability(cd_cargo: int | None) -> HistoryAvailability:
    if cd_cargo is None:
        return HistoryAvailability.no_public_source
    return _HISTORY.get(cd_cargo, HistoryAvailability.no_public_source)


def history_note(cd_cargo: int | None) -> str | None:
    """Caveat copy for this office's track record, or None when the source is complete.

    `partial` offices keep their note even though data IS shown — the caveat is the
    whole point there, since the numbers are not comparable across houses."""
    availability = history_availability(cd_cargo)
    if availability is HistoryAvailability.available:
        return None
    if availability is HistoryAvailability.not_applicable:
        return _EXECUTIVE_NOTE
    return HISTORY_NOTE.get(cd_cargo, "Sem fonte pública de histórico para este cargo.")


def shows_track_record(cd_cargo: int | None) -> bool:
    """Whether a confirmed incumbent in this office gets a track-record section."""
    return history_availability(cd_cargo) in (
        HistoryAvailability.available,
        HistoryAvailability.partial,
    )


def house_for(cd_cargo: int | None) -> House | None:
    return CARGO_HOUSE.get(cd_cargo) if cd_cargo is not None else None


# ── Executive terms ──────────────────────────────────────────────────────────
# A general election is held every four years and the winner takes office on 1 January
# of the following year (CF art. 28: "posse em 1º de janeiro do ano subsequente"),
# serving four years. Both collectors need the same window, and deriving it twice from
# the election year is how the two drift apart.
TERM_YEARS = 4


def previous_general_election(election_year: int) -> int:
    """The election that seated the incumbents running in `election_year`."""
    return election_year - TERM_YEARS


def executive_term(won_at_election: int) -> tuple[dt.date, dt.date]:
    """``(posse, fim)`` of the term won at `won_at_election`.

    ``executive_term(2022)`` -> ``(2023-01-01, 2026-12-31)``. The argument is the year
    of the election that CONFERRED the office, not the current cycle — pass
    `previous_general_election(settings.election_year)` to get the term of the people
    running for re-election now.

    🚨 `fim` is the constitutional end of the term and is **not** written to
    `Mandate.data_fim`. Across this schema `data_fim IS NULL` is what "currently in
    office" means (`resolution.records.load_person_recs`), so stamping the future end
    date on a governor still serving would mark them as a former office-holder and
    silently undo the incumbency it was added to establish. The window is for
    *querying the source*; the seat's occupancy is a separate claim.
    """
    start_year = won_at_election + 1
    return dt.date(start_year, 1, 1), dt.date(start_year + TERM_YEARS - 1, 12, 31)


def parse_cargos(raw: str | None) -> frozenset[int]:
    """Parse a `RESUMO_TARGET_CARGOS`-style spec ("3,5,6,7"). Empty/None = all."""
    if not raw or not raw.strip():
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            out.add(int(part))
            continue
        # Also accept names ("DEPUTADO FEDERAL") for readability in .env.
        wanted = part.upper()
        match = next((c for c, n in CARGO_NAMES.items() if n == wanted), None)
        if match is None:
            raise ValueError(f"cargo desconhecido: {part!r}")
        out.add(match)
    return frozenset(out)
