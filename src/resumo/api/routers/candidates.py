"""JSON read API for candidates."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from resumo.api import queries
from resumo.api.deps import get_session
from resumo.config import get_settings

router = APIRouter(prefix="/api/candidates", tags=["candidates"])


@router.get("")
def list_candidates(
    q: str | None = Query(default=None, description="name search (accent-insensitive)"),
    uf: str | None = None,
    cargo: str | None = None,
    partido: str | None = Query(default=None, description="sigla exata do partido"),
    reeleicao: bool | None = Query(
        default=None,
        description=(
            "true = incumbência confirmada (link de tier aceito); false = sem "
            "incumbência confirmada — o que inclui candidaturas ainda em revisão"
        ),
    ),
    year: int | None = Query(
        default=None, description="ano da eleição; omitido = ano configurado no deploy"
    ),
    limit: int = Query(default=50, le=200),
    session: Session = Depends(get_session),
) -> list[dict]:
    # Defaults to the deploy's election year so the public surface never mixes in the
    # historical validation set. Still overridable: an auditor comparing 2026 against
    # 2022 is a legitimate use, an accidental unscoped listing is not.
    return queries.search_candidacies(
        session,
        q=q,
        uf=uf,
        cargo=cargo,
        partido=partido,
        reeleicao=reeleicao,
        year=get_settings().election_year if year is None else year,
        limit=limit,
    )


@router.get("/{sq_candidato}")
def get_candidate(sq_candidato: str, session: Session = Depends(get_session)) -> dict:
    detail = queries.candidate_detail(session, sq_candidato)
    if detail is None:
        raise HTTPException(status_code=404, detail="candidatura não encontrada")
    return detail
