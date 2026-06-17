"""Deterministic resolution rules (R1, R2/R3 título, R4 handled at seed time).

R1 CPF exact is the dominant path for this build (TSE 2022 and Câmara detail both
carry CPF). Título is the cross-election anchor used when CPF is masked.
"""

from __future__ import annotations

from dataclasses import dataclass

from resumo.db.models import MatchMethod
from resumo.resolution.blocking import PersonIndex
from resumo.resolution.records import CandRec, PersonRec


@dataclass
class DeterministicHit:
    person: PersonRec
    method: MatchMethod
    score: float = 1.0


def match(cand: CandRec, index: PersonIndex) -> DeterministicHit | None:
    # R1 — CPF exact.
    if cand.cpf and cand.cpf in index.by_cpf:
        return DeterministicHit(index.by_cpf[cand.cpf], MatchMethod.cpf_exact)
    # R2/R3 — título exact (only useful once persons carry título; reserved hook).
    if cand.titulo:
        for p in index.candidates_in_uf(cand.uf):
            p_titulo = getattr(p, "titulo", None)
            if p_titulo and p_titulo == cand.titulo:
                return DeterministicHit(p, MatchMethod.titulo_exact)
    return None
