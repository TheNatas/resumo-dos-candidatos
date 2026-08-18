"""The author bridge: SIOP author code (per year) -> the mandate that authored it.

The emendas source carries **no CPF and no Câmara/Senado id**. An author is a
4-digit SIOP code plus a display name, so this edge has to be *built* — and, like
:class:`~resumo.db.models.CandidateMandateLink`, it is built ONCE, materialized with
its method/confidence/provenance, and then read as data. Nothing here ever runs at
request time, and a human decision (``match_method = manual``) is never clobbered.

Verified properties of the SIOP code that this module encodes:

* the code is embedded in the emenda code — ``202543010001`` = ``2025`` + ``4301``
  (author) + ``0001``;
* the namespace is partitioned by first digit (1-4, 9 = individual legislators,
  5-6 = comissões, 7 = bancadas, 8 = relator-geral). Verified to hold for all
  94,304 rows of the national file, so it doubles as a structural sanity check;
* 🚨 the code is stable per **MANDATE, not per person**: Esperidião Amin is ``2850``
  (2015-2019, deputy) and ``2210`` (2020-2026, senator). Hence the ``(code, ano)``
  grain of :class:`~resumo.db.models.AmendmentAuthorLink`;
* 🚨 a departed member's emendas are reassigned to their successor while keeping the
  original code: ``2925`` reads ``CARMEN ZANOTTO`` up to 2024 and ``GEOVANIA DE SA
  (EX-PARLAMENTAR CARMEN ZANOTTO, ...)`` in 2025, while the same Geovania separately
  owns ``3235``. One person legitimately holds two codes in one year, so nothing
  here assumes a 1:1 code<->person map. The parenthetical is stripped and the name
  in front of it — the current holder, per the source — is the one matched;
* names drift for a stable code (``5004`` is both ``COM. CULTURA`` and ``COMISSAO DE
  CULTURA - CCULT``), so the **code is the key and the name is display text only**.

Matching rule (deliberately narrow): the normalized author name must match
``Mandate.nome_parlamentar`` **exactly**, scoped to the UF the emenda applies to.
UF scoping is what makes exactness safe — without it there are real false positives
in this data (``Jussara Lima`` -> ``ANA PAULA LIMA``, ``Rodrigo Pacheco`` ->
``RODRIGO COELHO``). With it, SC deputies matched 16/16. Anything ambiguous (zero or
more than one surviving candidate mandate) is left **unlinked and reported** — never
guessed. Only individual (RP6) amendments are considered; bancada/comissão/relator
stay unlinked by construction.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from resumo.db.models import (
    AmendmentAuthorLink,
    AmendmentType,
    BudgetAmendment,
    ConfidenceTier,
    House,
    Mandate,
    MatchMethod,
)
from resumo.ingestion.emendas import parsing
from resumo.ingestion.ledger import upsert
from resumo.util import clean, normalize_name

logger = logging.getLogger("resumo.ingestion.emendas")

RESOLVER_VERSION = "emendas-author-v1"

INDIVIDUAL_TYPES = (
    AmendmentType.individual_finalidade_definida,
    AmendmentType.individual_transferencia_especial,
)

# Reference points for "which budget years does this mandate cover", used ONLY to
# break a tie between two same-name mandates in the same UF (e.g. the same person's
# Câmara and Senado terms). Câmara legislatura 57 = 2023-2027; ALESC's 20th = idem.
_LEGISLATURE_REF: dict[House, tuple[int, int]] = {
    House.CAMARA: (57, 2023),
    House.ASSEMBLEIA: (20, 2023),
}


@dataclass
class MandateRec:
    mandate_id: object
    person_id: object
    nome_norm: str
    uf: str
    house: House
    id_legislatura: int | None
    ano_inicio: int | None
    ano_fim: int | None

    def covers(self, ano: int) -> bool:
        """Whether the mandate plausibly held the seat during budget year `ano`.

        Dates win when present; otherwise the legislature window is derived for the
        houses whose numbering we know. When nothing is knowable we answer True —
        this filter may only ever break ties, never invent an exclusion.
        """
        if self.ano_inicio is not None or self.ano_fim is not None:
            if self.ano_inicio is not None and ano < self.ano_inicio:
                return False
            if self.ano_fim is not None and ano > self.ano_fim:
                return False
            return True
        ref = _LEGISLATURE_REF.get(self.house)
        if ref and self.id_legislatura:
            ref_leg, ref_year = ref
            start = ref_year + (self.id_legislatura - ref_leg) * 4
            return start <= ano <= start + 3
        return True


@dataclass
class AuthorBridgeResult:
    codes: int = 0
    linked: int = 0
    unlinked: int = 0
    amendments_linked: int = 0
    manual_kept: int = 0
    unresolved: list[tuple[str, int, str | None, str]] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"author codes={self.codes}: linked={self.linked}, unlinked={self.unlinked}"
            f" (manual kept={self.manual_kept}) · amendments attributed="
            f"{self.amendments_linked}"
        )


def display_name(raw: str | None) -> str | None:
    """The effective author name for matching.

    Succession is published as an annotation: ``GEOVANIA DE SA (EX-PARLAMENTAR CARMEN
    ZANOTTO, NOS TERMOS ART. 78 LDO 2025 ...)``. The name in front of the parenthesis
    is the parliamentarian who now holds the code, which is who the source is
    attributing the execution to; the full string stays in `author_name_raw` so the
    reassignment remains auditable.
    """
    v = clean(raw)
    if v is None:
        return None
    return clean(v.split("(", 1)[0])


def _mandate_index(session: Session) -> dict[tuple[str, str], list[MandateRec]]:
    """(UF sigla, normalized nome parlamentar) -> mandates."""
    index: dict[tuple[str, str], list[MandateRec]] = defaultdict(list)
    rows = session.execute(
        select(
            Mandate.id,
            Mandate.person_id,
            Mandate.nome_parlamentar,
            Mandate.sigla_uf,
            Mandate.house,
            Mandate.id_legislatura,
            Mandate.data_inicio,
            Mandate.data_fim,
        )
    )
    for mid, pid, nome, uf, house, leg, inicio, fim in rows:
        nome_norm = normalize_name(nome)
        uf_sigla = (clean(uf) or "").upper()
        if not nome_norm or not uf_sigla:
            continue
        index[(uf_sigla, nome_norm)].append(
            MandateRec(
                mandate_id=mid,
                person_id=pid,
                nome_norm=nome_norm,
                uf=uf_sigla,
                house=house,
                id_legislatura=leg,
                ano_inicio=inicio.year if inicio else None,
                ano_fim=fim.year if fim else None,
            )
        )
    return index


def _author_groups(session: Session, *, anos: list[int] | None) -> dict[tuple[str, int], dict]:
    """Distinct (SIOP code, ano) authors of *individual* amendments, with their
    display names and the UFs their emendas landed in."""
    stmt = (
        select(
            BudgetAmendment.siop_author_code,
            BudgetAmendment.ano,
            BudgetAmendment.author_name_raw,
            BudgetAmendment.uf,
            func.count().label("n"),
        )
        .where(
            BudgetAmendment.tipo.in_(INDIVIDUAL_TYPES),
            BudgetAmendment.siop_author_code.is_not(None),
        )
        .group_by(
            BudgetAmendment.siop_author_code,
            BudgetAmendment.ano,
            BudgetAmendment.author_name_raw,
            BudgetAmendment.uf,
        )
    )
    if anos:
        stmt = stmt.where(BudgetAmendment.ano.in_(anos))

    groups: dict[tuple[str, int], dict] = {}
    for code, ano, nome_raw, uf, n in session.execute(stmt):
        g = groups.setdefault(
            (code, int(ano)), {"names": Counter(), "ufs": set(), "raw": None, "rows": 0}
        )
        g["rows"] += n
        if nome_raw:
            g["names"][nome_raw] += n
        sigla = parsing.sigla_for_uf(uf)
        if sigla:
            g["ufs"].add(sigla)
    for g in groups.values():
        g["raw"] = g["names"].most_common(1)[0][0] if g["names"] else None
    return groups


def _manual_links(session: Session) -> set[tuple[str, int]]:
    rows = session.execute(
        select(AmendmentAuthorLink.siop_author_code, AmendmentAuthorLink.ano).where(
            AmendmentAuthorLink.match_method == MatchMethod.manual
        )
    )
    return {(code, int(ano)) for code, ano in rows}


def _backfill(session: Session, *, anos: list[int] | None) -> int:
    """Copy the resolved author onto the amendments themselves (individual only)."""
    stmt = (
        update(BudgetAmendment)
        .where(
            BudgetAmendment.siop_author_code == AmendmentAuthorLink.siop_author_code,
            BudgetAmendment.ano == AmendmentAuthorLink.ano,
            AmendmentAuthorLink.mandate_id.is_not(None),
            BudgetAmendment.tipo.in_(INDIVIDUAL_TYPES),
        )
        .values(
            person_id=AmendmentAuthorLink.person_id,
            mandate_id=AmendmentAuthorLink.mandate_id,
        )
        .execution_options(synchronize_session=False)
    )
    if anos:
        stmt = stmt.where(BudgetAmendment.ano.in_(anos))
    return session.execute(stmt).rowcount or 0


def resolve_authors(
    session: Session,
    *,
    anos: list[int] | None = None,
    resolver: str = RESOLVER_VERSION,
) -> AuthorBridgeResult:
    """Materialize AmendmentAuthorLink, then backfill person/mandate on the rows."""
    result = AuthorBridgeResult()
    index = _mandate_index(session)
    manual = _manual_links(session)
    link_rows: list[dict] = []

    def unresolved(code: str, ano: int, nome: str | None, reason: str) -> None:
        result.unlinked += 1
        result.unresolved.append((code, ano, nome, reason))

    for (code, ano), group in sorted(_author_groups(session, anos=anos).items()):
        result.codes += 1
        nome_raw = group["raw"]
        if (code, ano) in manual:
            # A human decided this edge. Leave it exactly as it is.
            result.manual_kept += 1
            result.linked += 1
            continue

        # Structural guard: an individual amendment authored by a 5xxx/7xxx code
        # would mean the type<->namespace correspondence broke. Never link it.
        if not parsing.is_structurally_individual(code):
            kind = parsing.author_code_kind(code)
            logger.warning(
                "emendas: individual amendment with a %s author code %r (%s) — not linked",
                kind, code, nome_raw,
            )
            unresolved(code, ano, nome_raw, f"código {code} outside the individual namespace ({kind})")
            continue

        nome_norm = normalize_name(display_name(nome_raw))
        if not nome_norm:
            unresolved(code, ano, nome_raw, "no author name")
            continue
        if not group["ufs"]:
            # Only "Múltiplo"/"Sem informação" localities: no UF to scope by, and an
            # unscoped exact name match is exactly the false-positive trap.
            unresolved(code, ano, nome_raw, "no UF scope (emendas are Múltiplo/sem informação)")
            continue

        candidates: dict[object, MandateRec] = {}
        for uf in sorted(group["ufs"]):
            for rec in index.get((uf, nome_norm), ()):
                candidates[rec.mandate_id] = rec

        if not candidates:
            ufs = ",".join(sorted(group["ufs"]))
            unresolved(code, ano, nome_raw, f"no mandate named {nome_norm!r} in uf={ufs}")
            continue
        if len(candidates) > 1:
            # Same name, same UF: usually one person's Câmara *and* Senado terms.
            # Narrowing by the budget year decides it; if it doesn't, stay unlinked.
            narrowed = [r for r in candidates.values() if r.covers(ano)]
            if len(narrowed) != 1:
                unresolved(
                    code, ano, nome_raw,
                    f"ambiguous: {len(candidates)} mandates match in the same UF",
                )
                continue
            chosen = narrowed[0]
        else:
            chosen = next(iter(candidates.values()))

        link_rows.append(
            {
                "siop_author_code": code,
                "ano": ano,
                "mandate_id": chosen.mandate_id,
                "person_id": chosen.person_id,
                "author_name_raw": nome_raw,
                # No CPF/título exists on this side, so an exact UF-scoped name match
                # is the strongest evidence available — recorded as such, not as a
                # deterministic identifier match.
                "match_method": MatchMethod.probabilistic,
                "confidence_score": 1.0,
                "confidence_tier": ConfidenceTier.auto_strong,
                "resolver": resolver,
            }
        )
        result.linked += 1

    upsert(
        session,
        AmendmentAuthorLink,
        link_rows,
        index_elements=["siop_author_code", "ano"],
        update_columns=[
            "mandate_id", "person_id", "author_name_raw", "match_method",
            "confidence_score", "confidence_tier", "resolver",
        ],
    )
    session.flush()
    result.amendments_linked = _backfill(session, anos=anos)

    for code, ano, nome, reason in result.unresolved:
        logger.info("emendas: author %s/%s (%s) left unlinked — %s", code, ano, nome, reason)
    logger.info("emendas author bridge: %s", result)
    return result
