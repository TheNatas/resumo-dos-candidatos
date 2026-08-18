"""Cargo taxonomy — TSE office codes and what each office implies.

Two distinctions the platform depends on and that are easy to conflate:

1. **Sistema eleitoral** (majoritário vs proporcional) — an electoral-math property.
   Senador is *majoritário* but files no proposta de governo.
2. **Proposta de governo obrigatória** — only the *executive* majoritarian offices
   (Presidente, Governador, Prefeito) must file one (Lei 9.504/97, art. 11 §1º XI).

So `is_majoritario` and "has a proposta" are NOT the same predicate; keeping them
apart is what lets the front say the right thing for a senador.

3. **Fonte de histórico** — whether a track record can exist *at all* for the office,
   and from which house. This drives the honest three-way message on the ficha:
   "histórico disponível" vs "não há fonte pública" vs "não se aplica a cargo executivo".
"""

from __future__ import annotations

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


# Which house holds the track record for an office, if any.
CARGO_HOUSE: dict[int, House] = {
    DEPUTADO_FEDERAL: House.CAMARA,
    SENADOR: House.SENADO,
    DEPUTADO_ESTADUAL: House.ASSEMBLEIA,
}

_HISTORY: dict[int, HistoryAvailability] = {
    PRESIDENTE: HistoryAvailability.not_applicable,
    VICE_PRESIDENTE: HistoryAvailability.not_applicable,
    GOVERNADOR: HistoryAvailability.not_applicable,
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
