"""FastAPI app: JSON API (/api/...) + server-rendered public front (Jinja + htmx)."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from resumo.api import queries
from resumo.api.deps import get_session
from resumo.api.routers import candidates
from resumo.config import get_settings

_WEB = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB / "templates"))

app = FastAPI(title="Resumo dos Candidatos", version="0.1.0")
app.include_router(candidates.router)
app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "election_year": get_settings().election_year}


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str | None = None,
    uf: str | None = None,
    session: Session = Depends(get_session),
):
    results = queries.search_candidacies(session, q=q, uf=uf, limit=50) if (q or uf) else []
    template = "_results.html" if request.headers.get("HX-Request") else "index.html"
    return templates.TemplateResponse(
        request, template, {"results": results, "q": q or "", "uf": uf or ""}
    )


@app.get("/candidato/{sq_candidato}", response_class=HTMLResponse)
def candidate_page(
    request: Request, sq_candidato: str, session: Session = Depends(get_session)
):
    detail = queries.candidate_detail(session, sq_candidato)
    if detail is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return templates.TemplateResponse(request, "candidate.html", {"d": detail})
