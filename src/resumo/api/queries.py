"""Read queries for the public surface.

Track record is gated: it is returned ONLY when an accepted-tier CandidateMandateLink
confirms incumbent-reelection — never a guessed link.

When it is absent, the reason matters and is reported as `history_status`, because
the three cases mean completely different things to a reader:

- ``available``          — a source exists; this person simply is not a confirmed
                           incumbent (or the link is still in review).
- ``not_applicable``     — executive office; no roll-call record exists by nature.
- ``no_public_source``   — the office HAS a record but nobody publishes it openly
                           (state assemblies). Silence here is about the source,
                           not about the candidate, and must be said out loud.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resumo import cargos
from resumo.db.models import (
    AccountFiling,
    AmendmentType,
    AttendanceRecord,
    BudgetAmendment,
    CampaignExpense,
    CampaignRevenue,
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


def candidacies_in_scope(
    session: Session,
    *,
    year: int,
    ufs: Sequence[str] = (),
    cargo_codes: Sequence[int] = (),
) -> list[dict]:
    """Every candidacy this deploy covers, as summaries.

    The static renderer's source of truth for which pages exist. Deliberately not
    `search_candidacies` with a big limit: which pages get published is an explicit
    scoped query, never whatever a search box happened to ask for.
    """
    stmt = select(Candidacy).where(Candidacy.ano_eleicao == year)
    if ufs:
        stmt = stmt.where(Candidacy.sg_uf.in_([u.upper() for u in ufs]))
    if cargo_codes:
        stmt = stmt.where(Candidacy.cd_cargo.in_(list(cargo_codes)))
    stmt = stmt.order_by(Candidacy.nome_candidato, Candidacy.sq_candidato)
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
    # Only rows where presence is actually known. A NULL `presente` means the source
    # published a code it does not define; counting it in the denominator would turn
    # "we don't know" into a falta.
    events_total = session.scalar(
        select(func.count()).select_from(AttendanceRecord).where(
            AttendanceRecord.mandate_id == mandate_id,
            AttendanceRecord.presente.isnot(None),
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


# Filings that represent money actually declared for this campaign. The FINAL file
# republishes earlier parcial rows, so summing across every filing type would count
# the same money twice.
_COUNTED_FILINGS = (AccountFiling.final, AccountFiling.parcial, AccountFiling.relatorio_financeiro)


def campaign_finance_summary(session: Session, sq: str) -> dict:
    """Money raised and spent by THIS candidacy.

    Unlike the track record, this is not gated on incumbency — every candidate files
    accounts. It is gated on the data existing at all: TSE publishes nothing until
    the first filing window, so `has_data` distinguishes "declared zero" from
    "has not filed yet", which look identical if you only report a total.
    """
    rev_total, rev_count = session.execute(
        select(func.coalesce(func.sum(CampaignRevenue.vr_receita), 0), func.count()).where(
            CampaignRevenue.sq_candidato == sq,
            CampaignRevenue.tp_prestacao_contas.in_(_COUNTED_FILINGS),
        )
    ).one()
    exp_total, exp_count = session.execute(
        select(func.coalesce(func.sum(CampaignExpense.vr_despesa_contratada), 0), func.count()).where(
            CampaignExpense.sq_candidato == sq,
            CampaignExpense.tp_prestacao_contas.in_(_COUNTED_FILINGS),
        )
    ).one()

    filings = [
        f for (f,) in session.execute(
            select(CampaignRevenue.tp_prestacao_contas)
            .where(CampaignRevenue.sq_candidato == sq)
            .distinct()
        )
    ]
    return {
        "has_data": bool(rev_count or exp_count),
        "revenue_total": float(rev_total or 0),
        "revenue_count": rev_count or 0,
        "expense_total": float(exp_total or 0),
        "expense_count": exp_count or 0,
        "filings": sorted(f.value for f in filings if f),
        "note": (
            "Valores declarados à Justiça Eleitoral. A prestação final só é entregue "
            "após a eleição; até lá os números são parciais e sobem."
        ),
    }


def top_donors(session: Session, sq: str, limit: int = 5) -> list[dict]:
    # Prefer the Receita Federal name over the self-declared one — it is the
    # canonical spelling and keeps the same donor from splitting into several rows.
    donor = func.coalesce(CampaignRevenue.nm_doador_rfb, CampaignRevenue.nm_doador).label("doador")
    total = func.sum(CampaignRevenue.vr_receita).label("total")
    rows = session.execute(
        select(donor, total)
        .where(
            CampaignRevenue.sq_candidato == sq,
            CampaignRevenue.tp_prestacao_contas.in_(_COUNTED_FILINGS),
        )
        .group_by(donor)
        .order_by(total.desc())
        .limit(limit)
    ).all()
    return [{"nome": nome, "total": float(value or 0)} for nome, value in rows]


def amendments_summary(session: Session, mandate_id: uuid.UUID) -> dict:
    """Budget amendments authored by this mandate holder.

    Only *individual* amendment types are attributable to one legislator; bancada,
    comissão and relator amendments belong to a group and are excluded by
    construction rather than filtered in the UI.
    """
    individual = [t for t in AmendmentType if t.is_individual]
    empenhado, pago, count = session.execute(
        select(
            func.coalesce(func.sum(BudgetAmendment.valor_empenhado), 0),
            func.coalesce(func.sum(BudgetAmendment.valor_pago), 0),
            func.count(),
        ).where(
            BudgetAmendment.mandate_id == mandate_id,
            BudgetAmendment.tipo.in_(individual),
        )
    ).one()
    return {
        "count": count or 0,
        "empenhado_total": float(empenhado or 0),
        "pago_total": float(pago or 0),
        "note": (
            "Apenas emendas individuais (as de bancada, comissão e relator não são "
            "atribuíveis a um parlamentar). A fonte não publica 'valor autorizado'; "
            "o empenhado é o melhor indicador disponível."
        ),
    }


def candidate_detail(
    session: Session, sq: str, *, include_storage_path: bool = False
) -> dict | None:
    cand = get_candidacy(session, sq)
    if cand is None:
        return None
    accepted = get_accepted_link(session, sq)
    track = None
    link_info = None
    amendments = None
    if accepted:
        link, mandate = accepted
        link_info = {
            "match_method": link.match_method.value,
            "confidence_score": link.confidence_score,
            "confidence_tier": link.confidence_tier.value,
            "house": mandate.house.value,
            "house_label": mandate.house.label,
            "nome_parlamentar": mandate.nome_parlamentar,
            # A mandate can be held without being exercised — a senator licensed to
            # serve as governor still legally holds the seat, and the record is still
            # theirs, but calling that "em exercício" would be false.
            "situacao": mandate.situacao,
            # About the record's completeness, not about the candidate.
            "house_caveat": cargos.house_caveat(mandate.house),
            "em_exercicio": (mandate.situacao or "").lower().startswith("exerc"),
            # True when the mandate is in the same house the candidacy is seeking.
            # False is normal and interesting: a deputado federal running for senador.
            "same_office": cargos.house_for(cand.cd_cargo) is mandate.house,
        }
        track = track_record_summary(session, mandate.id)
        amendments = amendments_summary(session, mandate.id)
    return {
        "candidacy": _candidacy_summary(cand),
        "proposals": [
            # `storage_path` is a server filesystem path and must not leave the
            # process: it leaks the deploy's directory layout and gives a reader
            # nothing. The id is the public handle, and the PDF is served from it —
            # collecting the proposta and never letting anyone open it would defeat
            # the point of collecting it.
            {
                "id": str(p.id),
                "source": p.source,
                "filename": p.original_filename,
                "url": f"/proposta/{p.id}.pdf",
                # Build-time only: the static renderer needs to find the file on disk
                # to copy it. Underscored and opt-in so it can never reach the public
                # payload by default.
                **({"_storage_path": p.storage_path} if include_storage_path else {}),
            }
            for p in get_proposals(session, sq)
        ],
        "incumbent_confirmed": accepted is not None,
        "link": link_info,
        "track_record": track,
        "amendments": amendments,
        # Not gated on incumbency — every candidacy files campaign accounts.
        "campaign_finance": campaign_finance_summary(session, sq),
        "top_donors": top_donors(session, sq),
    }


def _candidacy_summary(c: Candidacy) -> dict:
    return {
        "sq_candidato": c.sq_candidato,
        "nome": c.nome_candidato,
        "nome_urna": c.nome_urna,
        "nome_normalizado": c.nome_normalizado,
        "ano": c.ano_eleicao,
        "cd_cargo": c.cd_cargo,
        "cargo": c.ds_cargo,
        "uf": c.sg_uf,
        "partido": c.sg_partido,
        "situacao": c.ds_situacao_candidatura,
        "resultado": c.ds_sit_tot_turno,
        "majoritario": c.is_majoritario,
        # Derived, not stored: "majoritário" and "files a proposta de governo" are
        # different predicates — a senador is the former but not the latter.
        "requires_proposta": cargos.requires_proposta(c.cd_cargo),
        "history_status": cargos.history_availability(c.cd_cargo).value,
        "history_note": cargos.history_note(c.cd_cargo),
    }


def _norm(q: str) -> str:
    from resumo.util import normalize_name

    return normalize_name(q) or q
