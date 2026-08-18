"""Resolution pipeline: candidacies -> CandidateMandateLink (+ ReviewQueue).

Order of precedence per candidacy:
  1. Honour decided manual overrides in ReviewQueue (authoritative; never clobbered).
  2. Deterministic CPF/título match -> auto_strong.
  3. Probabilistic name(+DOB) match within UF -> tier by score; homonyms -> review.

Only auto_strong/auto_weak/manual produce a public link. `review` rows wait in the
queue and are NOT shown publicly.

Candidacies are matched against mandates in EVERY collected house, not just the one
matching the office they seek — a deputado federal running for senador is the normal
case. Houses differ in what identity they publish (Câmara has CPF, the Senado has
none, ALESC has neither CPF nor birth date), so a match backed only by a name is
held to auto_weak; see resolution/probabilistic.py.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import (
    Candidacy,
    CandidateMandateLink,
    ConfidenceTier,
    MatchMethod,
    ReviewQueue,
    ReviewStatus,
)
from resumo.ingestion.ledger import upsert
from resumo.resolution import bridge, deterministic, probabilistic
from resumo.resolution.blocking import PersonIndex
from resumo.resolution.records import PersonRec, load_candidacy_recs, load_person_recs

logger = logging.getLogger("resumo.resolution")

PIPELINE_VERSION = "r1-prob-v1"

# Probabilistic score -> tier thresholds.
STRONG = 0.95
WEAK = 0.88
REVIEW = 0.80


class ResolutionResult:
    def __init__(self):
        self.links = 0
        self.auto_strong = 0
        self.auto_weak = 0
        self.review = 0
        self.unmatched = 0
        self.via_tse = 0
        # Mandatos cujo CPF recuperado fechou a identidade: nenhuma outra
        # candidatura pode ser proposta para eles pelo comparador de nomes.
        self.closed_mandates = 0

    def __str__(self) -> str:
        extra = f", cpf_via_tse={self.via_tse}" if self.via_tse else ""
        return (
            f"links={self.links} (strong={self.auto_strong}, weak={self.auto_weak}), "
            f"review={self.review}, unmatched={self.unmatched}{extra}"
        )


def _decided_overrides(session: Session) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """Returns (forced_match: sq -> mandate_id, rejected: {(sq, mandate_id)})."""
    forced: dict[str, str] = {}
    rejected: set[tuple[str, str]] = set()
    rows = session.execute(
        select(ReviewQueue.sq_candidato, ReviewQueue.mandate_id, ReviewQueue.status).where(
            ReviewQueue.status.in_([ReviewStatus.match, ReviewStatus.no_match])
        )
    )
    for sq, mandate_id, status in rows:
        if status == ReviewStatus.match:
            forced[sq] = str(mandate_id)
        else:
            rejected.add((sq, str(mandate_id)))
    return forced, rejected


def _cpf_conflict(ident: bridge.BridgedIdentity | None, cand_cpf: str | None) -> bool:
    """Um mandato com CPF conhecido está fechado para qualquer outro CPF."""
    return bool(ident and cand_cpf and ident.cpf != cand_cpf)


def resolve(session: Session, *, year: int | None = None) -> ResolutionResult:
    persons = load_person_recs(session)
    index = PersonIndex(persons)
    by_mandate = {str(p.mandate_id): p for p in persons}
    cands = load_candidacy_recs(session, year=year)
    forced, rejected = _decided_overrides(session)

    # CPF recuperado do histórico do TSE para as Casas que não publicam CPF. Serve
    # nos dois sentidos, e o negativo é o mais valioso: refuta de uma vez os pares
    # que a comparação de nomes propôs por acaso.
    bridged = bridge.recover_cpfs(session, before_year=year or get_settings().election_year)
    by_bridged_cpf: dict[str, list[str]] = {}
    for mandate_id, ident in bridged.items():
        by_bridged_cpf.setdefault(ident.cpf, []).append(mandate_id)

    result = ResolutionResult()
    result.closed_mandates = len(bridged)
    link_rows: list[dict] = []
    review_rows: list[dict] = []
    person_updates: list[tuple[str, object]] = []

    def emit_link(sq: str, p: PersonRec, method: MatchMethod, score: float, tier: ConfidenceTier):
        link_rows.append(
            {
                "sq_candidato": sq,
                "mandate_id": p.mandate_id,
                "person_id": p.person_id,
                "match_method": method,
                "confidence_score": score,
                "confidence_tier": tier,
                "is_incumbent_reelection": p.mandate_active,
                "pipeline_version": PIPELINE_VERSION,
                "resolver": "manual" if method == MatchMethod.manual else PIPELINE_VERSION,
            }
        )
        person_updates.append((sq, p.person_id))
        result.links += 1

    for cand in cands:
        sq = cand.sq_candidato

        # 1. Manual override wins.
        if sq in forced and forced[sq] in by_mandate:
            emit_link(sq, by_mandate[forced[sq]], MatchMethod.manual, 1.0, ConfidenceTier.auto_strong)
            continue

        # 2. Deterministic.
        det = deterministic.match(cand, index)
        if det and (sq, str(det.person.mandate_id)) not in rejected:
            emit_link(sq, det.person, det.method, det.score, ConfidenceTier.auto_strong)
            result.auto_strong += 1
            continue

        # 2b. CPF recuperado do histórico do TSE (Casas que não publicam CPF).
        if cand.cpf:
            alvos = [
                m
                for m in by_bridged_cpf.get(cand.cpf, [])
                if m in by_mandate and (sq, m) not in rejected
            ]
            if len(alvos) > 1:
                # A mesma pessoa com mandatos em legislaturas diferentes: o que
                # interessa ao leitor é o que ela exerce agora.
                ativos = [m for m in alvos if by_mandate[m].mandate_active]
                alvos = ativos if len(ativos) == 1 else []
            if len(alvos) == 1:
                emit_link(
                    sq, by_mandate[alvos[0]], MatchMethod.cpf_via_tse, 1.0,
                    ConfidenceTier.auto_strong,
                )
                result.auto_strong += 1
                result.via_tse += 1
                continue

        # 3. Probabilistic within UF.
        pool = []
        for p in index.candidates_in_uf(cand.uf):
            if (sq, str(p.mandate_id)) not in rejected:
                if _cpf_conflict(bridged.get(str(p.mandate_id)), cand.cpf):
                    # O documento já disse que não é a mesma pessoa; deixar o
                    # comparador de nomes propor o par de novo só encheria a fila
                    # de revisão com o que já está decidido.
                    continue
                pool.append(p)
        hit = probabilistic.best_match(cand, pool)
        if hit.person is None or hit.score < REVIEW:
            result.unmatched += 1
            continue

        if hit.is_homonym or hit.score < WEAK:
            reason = "homonym" if hit.is_homonym else f"low score {hit.score:.2f}"
            if not hit.corroborated:
                reason += " (nome apenas — sem CPF/nascimento)"
            review_rows.append(
                {
                    "sq_candidato": sq,
                    "mandate_id": hit.person.mandate_id,
                    "suggested_score": hit.score,
                    "reason": reason,
                    "candidate_snapshot": {
                        "sq": sq, "nome": cand.extra.get("nome"),
                        "partido": cand.extra.get("partido"), "uf": cand.uf,
                        "cargo": cand.extra.get("cargo"),
                    },
                    "mandate_snapshot": {
                        "member_id": hit.person.member_id, "uf": hit.person.uf,
                        "house": hit.person.house.value,
                        "corroborated": hit.corroborated,
                    },
                    "status": ReviewStatus.pending,
                }
            )
            result.review += 1
            continue

        # A name-only match can never reach auto_strong: score_pair already caps it
        # below STRONG, and this guard keeps the invariant explicit rather than
        # leaving it as an emergent property of two constants agreeing.
        tier = (
            ConfidenceTier.auto_strong
            if (hit.score >= STRONG and hit.corroborated)
            else ConfidenceTier.auto_weak
        )
        emit_link(sq, hit.person, MatchMethod.probabilistic, hit.score, tier)
        if tier == ConfidenceTier.auto_strong:
            result.auto_strong += 1
        else:
            result.auto_weak += 1

    # Persist. Links upsert idempotently; reviews never clobber decided rows.
    upsert(
        session,
        CandidateMandateLink,
        link_rows,
        index_elements=["sq_candidato", "mandate_id"],
        update_columns=[
            "person_id", "match_method", "confidence_score", "confidence_tier",
            "is_incumbent_reelection", "pipeline_version", "resolver",
        ],
    )
    upsert(session, ReviewQueue, review_rows, index_elements=["sq_candidato", "mandate_id"], update_columns=[])
    for sq, pid in person_updates:
        session.execute(update(Candidacy).where(Candidacy.sq_candidato == sq).values(person_id=pid))

    logger.info("resolution: %s", result)
    return result
