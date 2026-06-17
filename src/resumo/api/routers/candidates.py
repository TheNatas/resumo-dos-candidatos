"""JSON read API for candidates."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from resumo.api import queries
from resumo.api.deps import get_session

router = APIRouter(prefix="/api/candidates", tags=["candidates"])


@router.get("")
def list_candidates(
    q: str | None = Query(default=None, description="name search (accent-insensitive)"),
    uf: str | None = None,
    cargo: str | None = None,
    year: int | None = None,
    limit: int = Query(default=50, le=200),
    session: Session = Depends(get_session),
) -> list[dict]:
    return queries.search_candidacies(session, q=q, uf=uf, cargo=cargo, year=year, limit=limit)


@router.get("/{sq_candidato}")
def get_candidate(sq_candidato: str, session: Session = Depends(get_session)) -> dict:
    detail = queries.candidate_detail(session, sq_candidato)
    if detail is None:
        raise HTTPException(status_code=404, detail="candidatura não encontrada")
    return detail
