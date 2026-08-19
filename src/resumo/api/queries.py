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
from collections import Counter
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resumo import attendance as att
from resumo import cargos
from resumo.db.models import (
    AccountFiling,
    AmendmentType,
    AttendanceSummary,
    AttendanceUnit,
    BudgetAmendment,
    CampaignExpense,
    CampaignRevenue,
    Candidacy,
    CandidateMandateLink,
    CandidatePhoto,
    ConfidenceTier,
    Expense,
    GovernmentProposal,
    Mandate,
    MandateLeave,
    Proposition,
    Vote,
)
from resumo.util import initials, normalize_name

ACCEPTED_TIERS = (ConfidenceTier.auto_strong, ConfidenceTier.auto_weak)

# The same gate `candidate_detail` applies, as a correlated predicate so a listing
# can carry (and filter on) incumbency without a query per row. It is one-directional
# evidence: True means a confirmed incumbent seeking re-election, False means only
# that no accepted link says so — an unresolved or in-review candidacy lands there too.
_CONFIRMED_REELECTION = (
    select(1)
    .select_from(CandidateMandateLink)
    .where(
        CandidateMandateLink.sq_candidato == Candidacy.sq_candidato,
        CandidateMandateLink.is_incumbent_reelection.is_(True),
        CandidateMandateLink.confidence_tier.in_(ACCEPTED_TIERS),
    )
    .exists()
)


# Same shape as `_CONFIRMED_REELECTION`: a listing needs to know whether each row
# has a face without one query per card.
_HAS_PHOTO = (
    select(1)
    .select_from(CandidatePhoto)
    .where(CandidatePhoto.sq_candidato == Candidacy.sq_candidato)
    .exists()
)


def search_candidacies(
    session: Session,
    *,
    q: str | None = None,
    uf: str | None = None,
    cargo: str | None = None,
    partido: str | None = None,
    reeleicao: bool | None = None,
    year: int | None = None,
    limit: int = 50,
) -> list[dict]:
    stmt = select(
        Candidacy, _CONFIRMED_REELECTION.label("incumbent"), _HAS_PHOTO.label("has_photo")
    )
    if q:
        # Accent-insensitive substring match on the normalized name (trgm index).
        stmt = stmt.where(Candidacy.nome_normalizado.ilike(f"%{_norm(q)}%"))
    if uf:
        stmt = stmt.where(Candidacy.sg_uf == uf.upper())
    if cargo:
        stmt = stmt.where(Candidacy.ds_cargo.ilike(f"%{cargo}%"))
    if partido:
        # Exact sigla, not a substring: "PP" inside "PPS"/"PSDB" would silently widen
        # a filter the reader chose precisely.
        stmt = stmt.where(Candidacy.sg_partido == partido.upper())
    if reeleicao is not None:
        stmt = stmt.where(_CONFIRMED_REELECTION if reeleicao else ~_CONFIRMED_REELECTION)
    if year:
        stmt = stmt.where(Candidacy.ano_eleicao == year)
    stmt = stmt.order_by(Candidacy.nome_candidato).limit(limit)
    return [
        _candidacy_summary(c, incumbent_confirmed=incumbent, has_photo=has_photo)
        for c, incumbent, has_photo in session.execute(stmt)
    ]


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
    stmt = select(
        Candidacy, _CONFIRMED_REELECTION.label("incumbent"), _HAS_PHOTO.label("has_photo")
    ).where(Candidacy.ano_eleicao == year)
    if ufs:
        stmt = stmt.where(Candidacy.sg_uf.in_([u.upper() for u in ufs]))
    if cargo_codes:
        stmt = stmt.where(Candidacy.cd_cargo.in_(list(cargo_codes)))
    stmt = stmt.order_by(Candidacy.nome_candidato, Candidacy.sq_candidato)
    return [
        _candidacy_summary(c, incumbent_confirmed=incumbent, has_photo=has_photo)
        for c, incumbent, has_photo in session.execute(stmt)
    ]


def partidos_in_scope(
    session: Session,
    *,
    year: int,
    ufs: Sequence[str] = (),
    cargo_codes: Sequence[int] = (),
) -> list[str]:
    """Party siglas that actually have a candidacy in scope, sorted.

    The filter's options come from the data, never from a fixed party list: a party
    with nobody to show would be an option that only ever empties the page.
    """
    stmt = select(Candidacy.sg_partido).where(
        Candidacy.ano_eleicao == year, Candidacy.sg_partido.is_not(None)
    )
    if ufs:
        stmt = stmt.where(Candidacy.sg_uf.in_([u.upper() for u in ufs]))
    if cargo_codes:
        stmt = stmt.where(Candidacy.cd_cargo.in_(list(cargo_codes)))
    return sorted({p.strip() for p in session.execute(stmt.distinct()).scalars() if p.strip()})


def get_candidacy(session: Session, sq: str) -> Candidacy | None:
    return session.get(Candidacy, sq)


def get_proposals(session: Session, sq: str) -> list[GovernmentProposal]:
    return list(
        session.execute(
            select(GovernmentProposal).where(GovernmentProposal.sq_candidato == sq)
        ).scalars()
    )


def get_photo(session: Session, sq: str) -> CandidatePhoto | None:
    return session.get(CandidatePhoto, sq)


# ── Foto: de onde ela vem ────────────────────────────────────────────────────
# The photo the candidate filed at registration, republished from the TSE bundle.
# Credited on the page for a reason: it is an official document, not a press
# portrait the campaign chose, and a reader comparing it to a glossy poster should
# know which of the two they are looking at. No other image is ever substituted —
# a face pulled off social media would be the one unofficial thing on the site.
PHOTO_CREDIT = "Foto oficial de registro da candidatura (TSE)"


def _photo_payload(photo: CandidatePhoto | None, *, include_storage_path: bool) -> dict | None:
    if photo is None:
        return None
    return {
        "url": f"/foto/{photo.sq_candidato}.jpg",
        "source": photo.source,
        "media_type": photo.media_type,
        "credit": PHOTO_CREDIT,
        # Build-time only, exactly like the proposta payload: `storage_path` is a
        # server filesystem path and must never reach a reader.
        **({"_storage_path": photo.storage_path} if include_storage_path else {}),
    }


# ── Proposta: de quem é o documento ──────────────────────────────────────────
# Nothing in the TSE bulk data says "this PDF is the party's program rather than this
# candidate's": the per-UF zip only maps a file to a candidate. But the *same* file —
# byte-identical, same content hash — filed under two different candidacies cannot be
# specific to either one. That much is derived, not guessed, and it is worth saying
# before the reader clicks: a program written for the party reads very differently
# from one written for this candidacy.
_SCOPE_LABELS = {"party": "Documento do partido", "shared": "Documento compartilhado"}


def shared_proposal_scopes(
    session: Session, proposals: Sequence[GovernmentProposal], *, ano_eleicao: int | None
) -> dict[str, dict]:
    """Map content_hash -> sharing info, for the hashes more than one candidacy filed.

    A hash filed by a single candidacy is simply absent (the common case), so the
    caller reads "not in this dict" as "specific to this candidacy".

    Scoped to one election year on purpose: a candidate who re-files the same platform
    four years later is a *different* sq_candidato, and counting that as sharing would
    flag their own document as somebody else's.
    """
    hashes = {p.content_hash for p in proposals if p.content_hash}
    if not hashes:
        return {}

    stmt = (
        select(GovernmentProposal.content_hash, Candidacy.sq_candidato, Candidacy.sg_partido)
        .join(Candidacy, GovernmentProposal.sq_candidato == Candidacy.sq_candidato)
        .where(GovernmentProposal.content_hash.in_(hashes))
        .distinct()
    )
    if ano_eleicao is not None:
        stmt = stmt.where(Candidacy.ano_eleicao == ano_eleicao)

    holders: dict[str, dict[str, str | None]] = {}
    for content_hash, sq, partido in session.execute(stmt):
        holders.setdefault(content_hash, {})[sq] = partido

    scopes: dict[str, dict] = {}
    for content_hash, by_sq in holders.items():
        if len(by_sq) < 2:
            continue
        partidos = sorted({p for p in by_sq.values() if p})
        # One party behind every candidacy that filed it -> it is that party's
        # document. More than one -> still not this candidate's, but calling it a
        # coligação would be an inference the data does not carry, so it is reported
        # only as shared.
        scopes[content_hash] = {
            "scope": "party" if len(partidos) == 1 else "shared",
            "shared_with": len(by_sq) - 1,
            "partidos": partidos,
        }
    return scopes


def _proposal_scope_payload(info: dict | None) -> dict:
    """The reader-facing half of `shared_proposal_scopes`. States what was observed —
    the same file under N candidacies — and stops there: a shared PDF is a fact about
    the document, never a judgement about the candidate."""
    if info is None:
        return {"scope": "candidacy", "scope_label": None, "scope_note": None, "shared_with": 0}
    if info["scope"] == "party":
        de_quem = f" do {info['partidos'][0]}"
    elif info["partidos"]:
        de_quem = f" ({', '.join(info['partidos'])})"
    else:
        de_quem = ""
    total = info["shared_with"] + 1
    return {
        "scope": info["scope"],
        "scope_label": _SCOPE_LABELS[info["scope"]],
        "scope_note": (
            f"O mesmo arquivo foi apresentado por {total} candidaturas{de_quem} nesta "
            "eleição — o conteúdo não é específico desta candidatura."
        ),
        "shared_with": info["shared_with"],
    }


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


def _sum_optional(values: Sequence[int | None]) -> int | None:
    """Soma ignorando nulos, mas devolve ``None`` quando NENHUMA linha tem o número.

    A diferença é publicada: ``0`` quer dizer "a fonte contou e deu zero", ``None``
    quer dizer "esta fonte não publica essa distinção". Colapsar os dois em zero faria
    a ficha afirmar "nenhuma falta injustificada" para a ALESC, que simplesmente não
    classifica ausência.
    """
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def attendance_payload(session: Session, mandate_id: uuid.UUID) -> dict:
    """Frequência do mandato, **uma linha por unidade publicada pela fonte**.

    Nada é convertido de sessão para dia ou vice-versa: a ficha imprime o número com o
    substantivo da fonte. A Câmara publica as duas réguas e as duas aparecem; Senado e
    ALESC publicam sessões e só sessões. Ver :mod:`resumo.attendance`.

    Os anos são somados dentro de cada régua — cada ano traz o próprio denominador
    (que na Câmara já vem restrito ao período de exercício do parlamentar), então a
    soma continua sendo "quantas de quantas".
    """
    rows = list(
        session.execute(
            select(AttendanceSummary).where(
                AttendanceSummary.mandate_id == mandate_id,
                AttendanceSummary.ambito == att.AMBITO_PLENARIO,
            )
        ).scalars()
    )
    if not rows:
        return {"available": False, "rows": [], "anos": [], "note": None, "fonte": None}

    metrica = Counter(r.metrica for r in rows).most_common(1)[0][0]
    metric = att.metric_for(metrica)
    ordem = list(metric.unidades) if metric else [AttendanceUnit.DIA, AttendanceUnit.SESSAO]

    payload_rows = []
    for unidade in ordem:
        group = [r for r in rows if r.unidade is unidade]
        if not group:
            continue
        total = sum(r.total or 0 for r in group)
        presenca = sum(r.presenca or 0 for r in group)
        payload_rows.append(
            {
                "unidade": unidade.value,
                "unidade_label": unidade.label,
                "denominador": (metric.denominador.get(unidade) if metric else unidade.label),
                "total": total,
                "presenca": presenca,
                "ausencia_total": max(total - presenca, 0),
                "ausencia_justificada": _sum_optional([r.ausencia_justificada for r in group]),
                "ausencia_nao_justificada": _sum_optional(
                    [r.ausencia_nao_justificada for r in group]
                ),
                "ausencia_nao_classificada": _sum_optional(
                    [r.ausencia_nao_classificada for r in group]
                ),
                # O denominador varia de parlamentar para parlamentar (quem se licencia
                # tem menos sessões no período de exercício), então o absoluto sozinho
                # ordena mal — o percentual é o número comparável.
                "percentual_presenca": round(presenca * 100 / total, 1) if total else None,
                "anos": sorted({r.ano for r in group}),
            }
        )

    return {
        "available": bool(payload_rows),
        "rows": payload_rows,
        "anos": sorted({r.ano for r in rows}),
        "fonte": metric.fonte if metric else None,
        "derivada": metric.derived if metric else None,
        "note": metric.note if metric else None,
        "source_url": next((r.source_url for r in rows if r.source_url), None),
    }


def leaves_payload(session: Session, mandate_id: uuid.UUID) -> dict | None:
    """Licenças formais do mandato, em dias corridos — ou ``None`` quando não há.

    Fica fora da conta de presença de propósito: dias de calendário e sessões são
    réguas diferentes, e somá-las produziria um número que nenhuma fonte publica.
    """
    rows = list(
        session.execute(
            select(MandateLeave).where(MandateLeave.mandate_id == mandate_id)
        ).scalars()
    )
    if not rows:
        return None
    dias = sum(att.leave_days(r.data_inicio, r.data_fim) or 0 for r in rows)
    tipos = Counter(r.descricao_tipo for r in rows if r.descricao_tipo)
    return {
        "count": len(rows),
        "dias": dias,
        "tipos": [{"descricao": nome, "count": n} for nome, n in tipos.most_common()],
        "primeira": min((r.data_inicio for r in rows if r.data_inicio), default=None),
        "ultima": max((r.data_fim for r in rows if r.data_fim), default=None),
    }


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
    expense_total = session.scalar(
        select(func.coalesce(func.sum(Expense.valor_liquido), 0)).where(
            Expense.mandate_id == mandate_id
        )
    )
    return {
        "votes_total": votes_total or 0,
        "votes_sim": votes_sim or 0,
        "propositions_total": props or 0,
        "expense_total": float(expense_total or 0),
        "attendance": attendance_payload(session, mandate_id),
        "leaves": leaves_payload(session, mandate_id),
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
    photo = get_photo(session, sq)
    proposals = get_proposals(session, sq)
    # Which of these PDFs are shared with other candidacies — resolved once for the
    # whole list rather than per row.
    scopes = shared_proposal_scopes(session, proposals, ano_eleicao=cand.ano_eleicao)
    return {
        "candidacy": _candidacy_summary(
            cand, incumbent_confirmed=accepted is not None, has_photo=photo is not None
        ),
        "photo": _photo_payload(photo, include_storage_path=include_storage_path),
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
                # Whose document this is, when the same file turns up under more than
                # one candidacy — flagged next to the link so the reader knows before
                # opening it.
                **_proposal_scope_payload(scopes.get(p.content_hash)),
                # Build-time only: the static renderer needs to find the file on disk
                # to copy it. Underscored and opt-in so it can never reach the public
                # payload by default.
                **({"_storage_path": p.storage_path} if include_storage_path else {}),
            }
            for p in proposals
        ],
        "incumbent_confirmed": accepted is not None,
        "link": link_info,
        "track_record": track,
        "amendments": amendments,
        # Not gated on incumbency — every candidacy files campaign accounts.
        "campaign_finance": campaign_finance_summary(session, sq),
        "top_donors": top_donors(session, sq),
    }


def _candidacy_summary(
    c: Candidacy, *, incumbent_confirmed: bool, has_photo: bool = False
) -> dict:
    return {
        "sq_candidato": c.sq_candidato,
        "nome": c.nome_candidato,
        "nome_urna": c.nome_urna,
        # None when TSE published no photo for this candidacy — the page draws the
        # initials block instead of leaving a broken image where a face should be.
        "foto_url": f"/foto/{c.sq_candidato}.jpg" if has_photo else None,
        "iniciais": initials(c.nome_candidato or c.nome_urna),
        "nome_normalizado": c.nome_normalizado,
        "ano": c.ano_eleicao,
        "cd_cargo": c.cd_cargo,
        "cargo": c.ds_cargo,
        "uf": c.sg_uf,
        "partido": c.sg_partido,
        "situacao": c.ds_situacao_candidatura,
        # Gated exactly like the ficha's track record: never a guessed link.
        "incumbent_confirmed": bool(incumbent_confirmed),
        "resultado": c.ds_sit_tot_turno,
        "majoritario": c.is_majoritario,
        # Derived, not stored: "majoritário" and "files a proposta de governo" are
        # different predicates — a senador is the former but not the latter.
        "requires_proposta": cargos.requires_proposta(c.cd_cargo),
        "history_status": cargos.history_availability(c.cd_cargo).value,
        "history_note": cargos.history_note(c.cd_cargo),
    }


def _norm(q: str) -> str:
    return normalize_name(q) or q
