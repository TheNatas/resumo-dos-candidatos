"""Probabilistic fallback when no deterministic key matches.

Self-contained rapidfuzz scorer (works out of the box, no heavy deps). For
production-grade Fellegi-Sunter linkage at national scale, swap this for Splink
(`uv sync --extra resolution`) — the pipeline only needs `best_match`.

**Corroboration gate.** A name is not an identifier. Where the house publishes no
CPF, no título and no birth date (ALESC gives name only), a fuzzy match can silently
attach one person's history to another. Matches with nothing corroborating the name
are therefore sent to review instead of being published.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz.distance import JaroWinkler

from resumo.resolution.records import CandRec, PersonRec

# Retained as a score ceiling for callers that inspect name-only matches directly.
NAME_ONLY_CAP = 0.93


@dataclass
class ProbHit:
    person: PersonRec | None
    score: float
    is_homonym: bool  # multiple persons score ~equally high -> ambiguous
    corroborated: bool = True  # False when only the name backs the match


def score_pair(cand: CandRec, person: PersonRec) -> float:
    if not cand.nome_norm or not person.nome_norm:
        return 0.0
    score = JaroWinkler.similarity(cand.nome_norm, person.nome_norm)

    # Date of birth is a strong corroborator / discriminator.
    if cand.dob and person.dob:
        return min(1.0, score + 0.05) if cand.dob == person.dob else score * 0.6

    # Nothing but the name agreed. Cap it — see the module docstring.
    return min(score, NAME_ONLY_CAP)


def _corroborated(cand: CandRec, person: PersonRec) -> bool:
    return bool(cand.dob and person.dob) or bool(cand.cpf and person.cpf)


def best_match(cand: CandRec, persons: list[PersonRec], *, ambiguous_margin: float = 0.03) -> ProbHit:
    scored = sorted(((score_pair(cand, p), p) for p in persons), key=lambda t: t[0], reverse=True)
    if not scored:
        return ProbHit(None, 0.0, False)
    top_score, top_person = scored[0]
    homonym = (
        len(scored) > 1
        and top_score > 0.8
        and (top_score - scored[1][0]) < ambiguous_margin
    )
    return ProbHit(top_person, top_score, homonym, _corroborated(cand, top_person))
