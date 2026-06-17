"""Read queries for the public surface.

Track record is gated: it is returned ONLY when an accepted-tier CandidateMandateLink
confirms incumbent-reelection. Otherwise the caller shows "incumbência não confirmada"
— never a guessed link.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resumo.db.models import (
    AttendanceRecord,
    Candidacy,
    CandidateMandateLink,
    ConfidenceTier,
    Expense,
    GovernmentProposal,
    Mandate,
    Proposition,
    Vote,
)

ACCEPTED_TIERS = (ConfidenceTier.auto_strong, ConfidenceTier.auto_weak)


def search_candidacies(
    session: Session,
    *,
    q: str | None = None,
    uf: str | None = None,
    cargo: str | None = None,
    year: int | None = None,
    limit: int = 50,
) -> list[dict]:
    stmt = select(Candidacy)
    if q:
        # Accent-insensitive substring match on the normalized name (trgm index).
        stmt = stmt.where(Candidacy.nome_normalizado.ilike(f"%{_norm(q)}%"))
    if uf:
        stmt = stmt.where(Candidacy.sg_uf == uf.upper())
    if cargo:
        stmt = stmt.where(Candidacy.ds_cargo.ilike(f"%{cargo}%"))
    if year:
        stmt = stmt.where(Candidacy.ano_eleicao == year)
    stmt = stmt.order_by(Candidacy.nome_candidato).limit(limit)
    return [_candidacy_summary(c) for c in session.execute(stmt).scalars()]


def get_candidacy(session: Session, sq: str) -> Candidacy | None:
    return session.get(Candidacy, sq)


def get_proposals(session: Session, sq: str) -> list[GovernmentProposal]:
    return list(
        session.execute(
            select(GovernmentProposal).where(GovernmentProposal.sq_candidato == sq)
        ).scalars()
    )


def get_accepted_link(session: Session, sq: str) -> tuple[CandidateMandateLink, Mandate] | None:
    row = session.execute(
        select(CandidateMandateLink, Mandate)
        .join(Mandate, CandidateMandateLink.mandate_id == Mandate.id)
        .where(
            CandidateMandateLink.sq_candidato == sq,
            CandidateMandateLink.is_incumbent_reelection.is_(True),
            CandidateMandateLink.confidence_tier.in_(ACCEPTED_TIERS),
        )
        .order_by(CandidateMandateLink.confidence_score.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def track_record_summary(session: Session, mandate_id: uuid.UUID) -> dict:
    votes_total = session.scalar(
        select(func.count()).select_from(Vote).where(Vote.mandate_id == mandate_id)
    )
    votes_sim = session.scalar(
        select(func.count()).select_from(Vote).where(
            Vote.mandate_id == mandate_id, Vote.tipo_voto == "Sim"
        )
    )
    props = session.scalar(
        select(func.count()).select_from(Proposition).where(
            Proposition.authoring_mandate_id == mandate_id
        )
    )
    present = session.scalar(
        select(func.count()).select_from(AttendanceRecord).where(
            AttendanceRecord.mandate_id == mandate_id, AttendanceRecord.presente.is_(True)
        )
    )
    events_total = session.scalar(
        select(func.count()).select_from(AttendanceRecord).where(
            AttendanceRecord.mandate_id == mandate_id
        )
    )
    expense_total = session.scalar(
        select(func.coalesce(func.sum(Expense.valor_liquido), 0)).where(
            Expense.mandate_id == mandate_id
        )
    )
    return {
        "votes_total": votes_total or 0,
        "votes_sim": votes_sim or 0,
        "propositions_total": props or 0,
        "attendance_present": present or 0,
        "attendance_events": events_total or 0,
        "expense_total": float(expense_total or 0),
        "attendance_note": "Presença derivada de listas de eventos; faltas são estimadas.",
    }


def candidate_detail(session: Session, sq: str) -> dict | None:
    cand = get_candidacy(session, sq)
    if cand is None:
        return None
    accepted = get_accepted_link(session, sq)
    track = None
    link_info = None
    if accepted:
        link, mandate = accepted
        link_info = {
            "match_method": link.match_method.value,
            "confidence_score": link.confidence_score,
            "confidence_tier": link.confidence_tier.value,
            "house": mandate.house.value,
            "nome_parlamentar": mandate.nome_parlamentar,
        }
        track = track_record_summary(session, mandate.id)
    return {
        "candidacy": _candidacy_summary(cand),
        "proposals": [
            {"source": p.source, "filename": p.original_filename, "storage_path": p.storage_path}
            for p in get_proposals(session, sq)
        ],
        "incumbent_confirmed": accepted is not None,
        "link": link_info,
        "track_record": track,
    }


def _candidacy_summary(c: Candidacy) -> dict:
    return {
        "sq_candidato": c.sq_candidato,
        "nome": c.nome_candidato,
        "nome_urna": c.nome_urna,
        "ano": c.ano_eleicao,
        "cargo": c.ds_cargo,
        "uf": c.sg_uf,
        "partido": c.sg_partido,
        "situacao": c.ds_situacao_candidatura,
        "resultado": c.ds_sit_tot_turno,
        "majoritario": c.is_majoritario,
    }


def _norm(q: str) -> str:
    from resumo.util import normalize_name

    return normalize_name(q) or q
