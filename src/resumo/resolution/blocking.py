"""Blocking: cut the candidate x person comparison space.

Primary block is UF (cheap, high-recall). CPF and título are degenerate blocks that
short-circuit straight to a deterministic match.
"""

from __future__ import annotations

from collections import defaultdict

from resumo.resolution.records import PersonRec


class PersonIndex:
    def __init__(self, persons: list[PersonRec]):
        self.by_cpf: dict[str, PersonRec] = {}
        self.by_titulo: dict[str, PersonRec] = {}
        self.by_uf: dict[str, list[PersonRec]] = defaultdict(list)
        for p in persons:
            if p.cpf:
                self.by_cpf[p.cpf] = p
            if p.titulo:
                self.by_titulo[p.titulo] = p
            if p.uf:
                self.by_uf[p.uf].append(p)

    def candidates_in_uf(self, uf: str | None) -> list[PersonRec]:
        return self.by_uf.get(uf or "", [])
