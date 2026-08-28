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
from resumo import cargos, sources
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
    House,
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


# Which house the confirmed mandate sits in — the same link `get_accepted_link`
# picks (accepted tier, highest score), so a card and the ficha never name different
# mandates. NULL here is exactly `_CONFIRMED_REELECTION` being false.
_INCUMBENT_HOUSE = (
    select(Mandate.house)
    .select_from(CandidateMandateLink)
    .join(Mandate, CandidateMandateLink.mandate_id == Mandate.id)
    .where(
        CandidateMandateLink.sq_candidato == Candidacy.sq_candidato,
        CandidateMandateLink.is_incumbent_reelection.is_(True),
        CandidateMandateLink.confidence_tier.in_(ACCEPTED_TIERS),
    )
    .order_by(CandidateMandateLink.confidence_score.desc())
    .limit(1)
    .scalar_subquery()
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
        Candidacy,
        _CONFIRMED_REELECTION.label("incumbent"),
        _HAS_PHOTO.label("has_photo"),
        _INCUMBENT_HOUSE.label("incumbent_house"),
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
        _candidacy_summary(
            c, incumbent_confirmed=incumbent, has_photo=has_photo, incumbent_house=house
        )
        for c, incumbent, has_photo, house in session.execute(stmt)
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
        Candidacy,
        _CONFIRMED_REELECTION.label("incumbent"),
        _HAS_PHOTO.label("has_photo"),
        _INCUMBENT_HOUSE.label("incumbent_house"),
    ).where(Candidacy.ano_eleicao == year)
    if ufs:
        stmt = stmt.where(Candidacy.sg_uf.in_([u.upper() for u in ufs]))
    if cargo_codes:
        stmt = stmt.where(Candidacy.cd_cargo.in_(list(cargo_codes)))
    stmt = stmt.order_by(Candidacy.nome_candidato, Candidacy.sq_candidato)
    return [
        _candidacy_summary(
            c, incumbent_confirmed=incumbent, has_photo=has_photo, incumbent_house=house
        )
        for c, incumbent, has_photo, house in session.execute(stmt)
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
    # A ordem de exibição é a que a métrica declara, mas qualquer unidade presente no
    # banco e ausente dessa lista entra no fim: um número coletado não pode sumir da
    # ficha porque o registro da métrica ficou desatualizado.
    ordem = list(metric.unidades) if metric else []
    ordem += [u for u in (AttendanceUnit.DIA, AttendanceUnit.SESSAO) if u not in ordem]

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
    primeira = min((r.data_inicio for r in rows if r.data_inicio), default=None)
    ultima = max((r.data_fim for r in rows if r.data_fim), default=None)
    return {
        "count": len(rows),
        "dias": dias,
        "tipos": [{"descricao": nome, "count": n} for nome, n in tipos.most_common()],
        # Strings ISO, não `date`: o mesmo dict vai para a API e para o JSON estático
        # do site, e `json.dumps` não tem tipo de data. A ficha não imprime estes dois
        # campos — quem os lê é a API.
        "primeira": primeira.isoformat() if primeira else None,
        "ultima": ultima.isoformat() if ultima else None,
    }


def expenses_payload(session: Session, mandate_id: uuid.UUID, house: House) -> dict:
    """Gastos de gabinete do mandato — o total, o que ele cobre e contra o que comparar.

    Um total sozinho não informa nada. "R$ 926.077,54" só vira informação com três
    coisas ao lado, e nenhuma delas é opcional:

    * **O nome certo.** Ver :attr:`~resumo.db.models.House.expense_label`.
    * **A janela.** O total é a soma do que foi *coletado*, não do mandato inteiro —
      os anos vêm do próprio dado (``ano`` distinto), então a ficha nunca afirma
      cobertura que não tem. Quem lê um número sem janela assume o mandato todo.
    * **Uma régua.** Contra o que 926 mil é muito ou pouco?

    Sobre a régua: **nenhuma fonte ingerida publica teto de gasto.** A Câmara divulga
    a cota mensal por UF, mas fora da API — e o portal da ALESC não publica limite
    algum para estas rubricas (o valor de ~R$ 111 mil que circula é a verba de
    *pessoal*, para salários de secretários, que não é o que estas linhas medem).
    Cravar um denominador aqui seria inventar a parte mais importante da conta. Então
    a régua é a única que sai do dado que já temos: a **mediana da própria Casa na
    mesma janela** — comparação entre pares, sem afirmar teto nenhum.
    """
    rows = list(
        session.execute(
            select(
                Expense.tipo_despesa,
                func.count().label("n"),
                func.coalesce(func.sum(Expense.valor_liquido), 0).label("total"),
            )
            .where(Expense.mandate_id == mandate_id)
            .group_by(Expense.tipo_despesa)
            .order_by(func.coalesce(func.sum(Expense.valor_liquido), 0).desc())
        )
    )
    total = float(sum(r.total for r in rows))
    count = int(sum(r.n for r in rows))
    anos = [
        a
        for (a,) in session.execute(
            select(Expense.ano).where(Expense.mandate_id == mandate_id).distinct().order_by(Expense.ano)
        )
    ]

    # Mediana entre os pares da MESMA Casa: um deputado estadual não se compara a um
    # federal (regimes e rubricas diferentes), e a janela ingerida é a mesma para todos
    # os mandatos de uma Casa, então a comparação é entre iguais.
    peer_totals = [
        float(t)
        for (t,) in session.execute(
            select(func.coalesce(func.sum(Expense.valor_liquido), 0))
            .join(Mandate, Mandate.id == Expense.mandate_id)
            .where(Mandate.house == house)
            .group_by(Expense.mandate_id)
        )
    ]
    peer = None
    # Com menos de 3 pares a "mediana" é ruído com cara de estatística.
    if len(peer_totals) >= 3:
        ordered = sorted(peer_totals)
        mid = len(ordered) // 2
        median = (
            ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        )
        peer = {
            "n": len(ordered),
            "median": median,
            "max": ordered[-1],
            # Acima ou abaixo da mediana — não um ranking. Quem assumiu no meio da
            # janela ou se licenciou gasta menos sem que isso diga nada sobre gestão,
            # e um "12º lugar" leria como placar.
            "above_median": total > median,
        }

    return {
        "total": total,
        "count": count,
        "anos": anos,
        "label": house.expense_label,
        "by_tipo": [
            {"tipo": r.tipo_despesa or "—", "count": int(r.n), "total": float(r.total)}
            for r in rows
        ],
        "peer": peer,
        "quota_note": (
            "Nenhuma fonte aberta ingerida publica o teto de gasto desta Casa, então "
            "este valor é o que foi efetivamente reembolsado — não uma fração de uma "
            "cota. A comparação ao lado é entre pares da mesma Casa, na mesma janela."
        ),
    }


def track_record_summary(session: Session, mandate_id: uuid.UUID, house: House) -> dict:
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
    expenses = expenses_payload(session, mandate_id, house)
    return {
        "votes_total": votes_total or 0,
        "votes_sim": votes_sim or 0,
        "propositions_total": props or 0,
        # Mantido como estava para não quebrar quem já consome a API; o detalhe (nome
        # correto da rubrica, janela coberta, comparação entre pares) vive em
        # `expenses`, porque o total sozinho não é interpretável.
        "expense_total": expenses["total"],
        "expenses": expenses,
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


# ── Detalhe por trás de cada número da ficha ─────────────────────────────────
# "12 votos nominais" e "59 proposições" não afirmam nada sozinhos: sem poder ver EM
# QUE se votou, o número não distingue um mandato de outro. Cada contador da ficha
# aponta para a listagem que o sustenta, e as listagens saem das mesmas linhas que já
# foram coletadas — nada aqui é buscado de novo, só deixou de ser descartado.


def votes_detail(session: Session, mandate_id: uuid.UUID, house: House) -> list[dict]:
    """Cada voto nominal do mandato, do mais recente para o mais antigo."""
    rows = session.execute(
        select(Vote)
        .where(Vote.mandate_id == mandate_id)
        .order_by(Vote.data_votacao.desc().nullslast(), Vote.id_votacao)
    ).scalars()
    out = []
    for v in rows:
        # A orientação do partido é o que torna o voto legível: sozinho, "Sim" não
        # diz se o parlamentar seguiu a bancada ou rompeu com ela. A ALESC não publica
        # orientação para votação nenhuma, então lá a coluna fica vazia — e vazia é
        # diferente de "votou com o partido".
        seguiu = None
        if v.orientacao_partido and v.tipo_voto:
            seguiu = v.orientacao_partido.strip().lower() == v.tipo_voto.strip().lower()
        out.append(
            {
                "data": v.data_votacao,
                "id_votacao": v.id_votacao,
                "tipo_voto": v.tipo_voto,
                "orientacao_partido": v.orientacao_partido,
                "seguiu_orientacao": seguiu,
                "proposicao_id": v.id_proposicao,
                "proposicao_url": sources.proposition_url(house, v.id_proposicao),
            }
        )
    return out


def propositions_detail(session: Session, mandate_id: uuid.UUID, house: House) -> list[dict]:
    """Cada proposição de autoria do mandato, da mais recente para a mais antiga."""
    rows = session.execute(
        select(Proposition)
        .where(Proposition.authoring_mandate_id == mandate_id)
        .order_by(
            Proposition.data_apresentacao.desc().nullslast(),
            Proposition.ano.desc().nullslast(),
            Proposition.numero.desc().nullslast(),
        )
    ).scalars()
    return [
        {
            "id": p.proposition_id,
            "codigo": " ".join(
                part for part in (p.sigla_tipo, str(p.numero or ""), str(p.ano or "")) if part
            ).strip()
            or p.proposition_id,
            "ementa": p.ementa,
            "situacao": p.situacao,
            "data": p.data_apresentacao,
            "url": sources.proposition_url(house, p.proposition_id),
        }
        for p in rows
    ]


def expenses_detail(session: Session, mandate_id: uuid.UUID) -> list[dict]:
    """Cada lançamento de gasto de gabinete, do mais recente para o mais antigo."""
    rows = session.execute(
        select(Expense)
        .where(Expense.mandate_id == mandate_id)
        .order_by(Expense.ano.desc(), Expense.mes.desc().nullslast(), Expense.id)
    ).scalars()
    return [
        {
            "ano": e.ano,
            "mes": e.mes,
            "tipo": e.tipo_despesa,
            "fornecedor": e.nome_fornecedor,
            "cnpj_cpf": e.cnpj_cpf_fornecedor,
            "valor_liquido": float(e.valor_liquido or 0),
            # Glosa é o que a Casa recusou reembolsar. Some-la ao líquido ou escondê-la
            # apagaria a única pista de que a despesa foi contestada.
            "valor_glosa": float(e.valor_glosa or 0),
            "documento_url": e.url_documento,
        }
        for e in rows
    ]


# Uma seção = um contador da ficha. O registro é único para que rota, renderizador
# estático e template não possam divergir sobre quais páginas existem.
TRACK_SECTIONS: dict[str, dict] = {
    "votos": {
        "titulo": "Votos nominais",
        "vazio": "Nenhum voto nominal registrado para este mandato.",
    },
    "proposicoes": {
        "titulo": "Proposições",
        "vazio": "Nenhuma proposição de autoria registrada para este mandato.",
    },
    "gastos": {
        "titulo": "Gastos de gabinete",
        "vazio": "Nenhum lançamento de gasto registrado para este mandato.",
    },
}


def track_section(session: Session, sq: str, secao: str) -> dict | None:
    """Uma listagem de detalhe, ou None quando a seção ou a candidatura não existem.

    Passa pelo MESMO portão de `candidate_detail`: sem vínculo aceito não há histórico
    a mostrar, e uma URL de detalhe não pode virar a porta dos fundos para um vínculo
    que a ficha se recusa a afirmar.
    """
    if secao not in TRACK_SECTIONS:
        return None
    cand = get_candidacy(session, sq)
    if cand is None:
        return None
    accepted = get_accepted_link(session, sq)
    if accepted is None:
        return None
    link, mandate = accepted
    if secao == "votos":
        rows = votes_detail(session, mandate.id, mandate.house)
    elif secao == "proposicoes":
        rows = propositions_detail(session, mandate.id, mandate.house)
    else:
        rows = expenses_detail(session, mandate.id)
    return {
        "secao": secao,
        "titulo": TRACK_SECTIONS[secao]["titulo"],
        "vazio": TRACK_SECTIONS[secao]["vazio"],
        "candidacy": _candidacy_summary(cand, incumbent_confirmed=True, has_photo=False),
        "house_label": mandate.house.label,
        "house_caveat": cargos.house_caveat(mandate.house),
        "expense_label": mandate.house.expense_label,
        "nome_parlamentar": mandate.nome_parlamentar,
        "rows": rows,
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
        track = track_record_summary(session, mandate.id, mandate.house)
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
    c: Candidacy,
    *,
    incumbent_confirmed: bool,
    has_photo: bool = False,
    incumbent_house: House | None = None,
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
        # Which house that mandate sits in, and whether it is the office THIS candidacy
        # disputes. Not the same claim: an accepted link only says the person holds a
        # current mandate, and a deputado federal running for governador holds one
        # without seeking re-election. The ficha already separates them
        # (`link.same_office`); a listing that flattened both into one "reeleição" flag
        # would assert of a person something the data does not say.
        "incumbent_house": incumbent_house.label if incumbent_house else None,
        "reelection_same_office": (
            incumbent_house is not None and cargos.house_for(c.cd_cargo) is incumbent_house
        ),
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
