"""Manual review decisions as a versioned file.

A decision on the candidacy↔mandate link is the one piece of the pipeline that is a
human judgement rather than a derivation, so it is the one piece that must not live
only in a database. `review-decisions.yml` is the source of truth: the build applies
it before `resolve`, and every judgement lands in the git history under the same
scrutiny as the data it affects.

**The key is natural, never the queue id.** `review_queue.id` and `mandate.id` are
uuids generated at insert time — they change on every rebuild of the database, and a
file keyed by them would silently stop matching exactly when the pipeline recovers
from a cold start. `sq_candidato` comes from the TSE and
(`house`, `house_member_id`, `id_legislatura`) comes from the House itself; both
survive a rebuild because they are what the sources publish.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.db.models import Candidacy, House, Mandate, ReviewQueue, ReviewStatus

SCHEMA_VERSION = 1
_REQUIRED = ("sq_candidato", "house", "house_member_id", "id_legislatura", "decision")
# `pending` is the absence of a decision; writing it down would be a no-op dressed
# up as a judgement.
_ALLOWED = (ReviewStatus.match, ReviewStatus.no_match, ReviewStatus.uncertain)


class DecisionFileError(ValueError):
    """The file is malformed or self-contradictory. Always fatal: a decisions file
    that cannot be read in full must not be applied in part."""


@dataclass(frozen=True)
class Decision:
    sq_candidato: str
    house: House
    house_member_id: str
    id_legislatura: int
    decision: ReviewStatus
    by: str
    note: str | None = None

    @property
    def mandate_key(self) -> tuple[House, str, int]:
        return (self.house, self.house_member_id, self.id_legislatura)


@dataclass
class ApplyResult:
    applied: int = 0
    unchanged: int = 0
    missing_mandate: list[str] = field(default_factory=list)
    missing_queue: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.missing_mandate) + len(self.missing_queue)

    def __str__(self) -> str:
        return (
            f"review apply: {self.applied} aplicadas, {self.unchanged} sem mudança, "
            f"{self.skipped} ignoradas "
            f"({len(self.missing_mandate)} sem mandato, {len(self.missing_queue)} sem fila)"
        )


def _fail(index: int, message: str) -> None:
    raise DecisionFileError(f"decisions[{index}]: {message}")


def parse_decisions(raw: str) -> list[Decision]:
    """Parse and validate the whole file. Rejects contradictions rather than letting
    a later entry quietly win — two opposite judgements about the same pair mean the
    file is wrong, and guessing which one was meant is not our call."""
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise DecisionFileError("o arquivo deve ser um mapa com 'version' e 'decisions'")

    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise DecisionFileError(f"version {version!r} não suportada (esperada: {SCHEMA_VERSION})")

    entries = data.get("decisions") or []
    if not isinstance(entries, list):
        raise DecisionFileError("'decisions' deve ser uma lista")

    decisions: list[Decision] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _fail(i, "cada decisão deve ser um mapa")
        for required in _REQUIRED:
            if entry.get(required) in (None, ""):
                _fail(i, f"campo obrigatório ausente: {required}")
        # Case-insensitive on purpose: this file is hand-edited, and rejecting
        # "camara" for "CAMARA" would be pedantry, not validation.
        try:
            house = House(str(entry["house"]).strip().upper())
        except ValueError:
            _fail(i, f"house inválida: {entry['house']!r} (use: {[h.value for h in House]})")
        try:
            status = ReviewStatus(str(entry["decision"]).strip().lower())
        except ValueError:
            _fail(i, f"decision inválida: {entry['decision']!r}")
        if status not in _ALLOWED:
            _fail(i, f"decision deve ser uma de {[s.value for s in _ALLOWED]}")
        try:
            legislatura = int(entry["id_legislatura"])
        except (TypeError, ValueError):
            _fail(i, f"id_legislatura deve ser inteiro: {entry['id_legislatura']!r}")
        decisions.append(
            Decision(
                sq_candidato=str(entry["sq_candidato"]),
                house=house,
                house_member_id=str(entry["house_member_id"]),
                id_legislatura=legislatura,
                decision=status,
                by=str(entry.get("by") or "review-decisions.yml"),
                note=entry.get("note"),
            )
        )

    _reject_contradictions(decisions)
    return decisions


def _reject_contradictions(decisions: list[Decision]) -> None:
    pairs = Counter((d.sq_candidato, d.mandate_key) for d in decisions)
    for (sq, key), n in pairs.items():
        if n > 1:
            raise DecisionFileError(
                f"{sq} + {key[0].value}/{key[1]}: {n} decisões para o mesmo par"
            )
    # `resolve` forces at most one mandate per candidacy (forced: sq -> mandate_id),
    # so two `match` rows for one candidacy would make the outcome depend on row order.
    matches = Counter(d.sq_candidato for d in decisions if d.decision is ReviewStatus.match)
    for sq, n in matches.items():
        if n > 1:
            raise DecisionFileError(
                f"{sq}: {n} decisões 'match' — uma candidatura só pode ser vinculada "
                "a um mandato"
            )


def load_decisions(path: Path) -> list[Decision]:
    if not path.is_file():
        raise DecisionFileError(f"arquivo não encontrado: {path}")
    return parse_decisions(path.read_text(encoding="utf-8"))


def apply_decisions(
    session: Session, decisions: list[Decision], *, by_default: str = "review-decisions.yml"
) -> ApplyResult:
    """Write the file's judgements onto the review queue. Idempotent: re-applying an
    unchanged file touches nothing, which is what lets the build run daily."""
    result = ApplyResult()
    if not decisions:
        return result

    mandates = {
        (m.house, m.house_member_id, m.id_legislatura): m.id
        for m in session.execute(
            select(Mandate).where(
                Mandate.house.in_({d.house for d in decisions}),
                Mandate.house_member_id.in_({d.house_member_id for d in decisions}),
            )
        ).scalars()
    }
    now = dt.datetime.now(dt.UTC)

    for decision in decisions:
        mandate_id = mandates.get(decision.mandate_key)
        label = f"{decision.sq_candidato}+{decision.house.value}/{decision.house_member_id}"
        if mandate_id is None:
            # Mandates not collected yet (or the person left the House). Not an error
            # by itself: collectors run before this, but a cold start can race.
            result.missing_mandate.append(label)
            continue
        row = session.execute(
            select(ReviewQueue).where(
                ReviewQueue.sq_candidato == decision.sq_candidato,
                ReviewQueue.mandate_id == mandate_id,
            )
        ).scalar_one_or_none()
        if row is None:
            # The pipeline never flagged this pair. We refuse to invent a queue entry:
            # forcing a link nobody proposed would publish a human's guess as if the
            # pipeline had found it.
            result.missing_queue.append(label)
            continue
        if row.status is decision.decision and row.decided_by == (decision.by or by_default):
            result.unchanged += 1
            continue
        row.status = decision.decision
        row.decided_by = decision.by or by_default
        row.decided_at = now
        result.applied += 1

    return result


def export_pending(session: Session, *, limit: int | None = None) -> str:
    """Emit the pending queue in the file's own format, so a decision is made by
    filling in a field rather than by hand-assembling natural keys."""
    stmt = (
        select(ReviewQueue, Mandate, Candidacy)
        .join(Mandate, ReviewQueue.mandate_id == Mandate.id)
        .join(Candidacy, ReviewQueue.sq_candidato == Candidacy.sq_candidato)
        .where(ReviewQueue.status == ReviewStatus.pending)
        .order_by(ReviewQueue.suggested_score.desc().nullslast())
    )
    if limit:
        stmt = stmt.limit(limit)

    lines = [
        "# Gerado por `resumo review export` — preencha `decision` e `by`, e apague",
        "# as entradas que você não decidiu. Chave natural: sobrevive a um rebuild.",
        f"version: {SCHEMA_VERSION}",
        "decisions:",
    ]
    for queue_row, mandate, candidacy in session.execute(stmt):
        score = "—" if queue_row.suggested_score is None else f"{queue_row.suggested_score:.2f}"
        lines += [
            f"  # {candidacy.nome_candidato} ({candidacy.ds_cargo}/{candidacy.sg_uf})"
            f" ↔ {mandate.nome_parlamentar} ({mandate.house.value})"
            f" · score {score} · {queue_row.reason or 'sem motivo registrado'}",
            f'  - sq_candidato: "{candidacy.sq_candidato}"',
            f"    house: {mandate.house.value}",
            f'    house_member_id: "{mandate.house_member_id}"',
            f"    id_legislatura: {mandate.id_legislatura}",
            "    decision: uncertain   # match | no_match | uncertain",
            "    by: ",
            "    note: ",
        ]
    if len(lines) == 4:
        lines[-1] = "decisions: []"
    return "\n".join(lines) + "\n"
