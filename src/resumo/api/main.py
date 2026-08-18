"""FastAPI app: JSON API (/api/...) + server-rendered public front (Jinja + htmx)."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from resumo import cargos
from resumo.api import queries
from resumo.api.deps import get_session
from resumo.api.routers import candidates
from resumo.config import get_settings

_WEB = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB / "templates"))

app = FastAPI(title="Resumo dos Candidatos", version="0.1.0")
app.include_router(candidates.router)
app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")


def _scope() -> dict:
    """The install's declared scope, surfaced so the public page never implies it
    covers more than it does."""
    s = get_settings()
    ufs = s.uf_list
    codes = sorted(s.cargo_set) or sorted(cargos.CARGO_NAMES)
    return {
        "election_year": s.election_year,
        "ufs": list(ufs),
        "cargos": [{"cd_cargo": c, "nome": cargos.CARGO_NAMES.get(c, str(c))} for c in codes],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", **_scope()}


@app.get("/api/scope")
def scope() -> dict:
    """What this deployment actually covers (state + offices + election year)."""
    return _scope()


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str | None = None,
    uf: str | None = None,
    cargo: str | None = None,
    session: Session = Depends(get_session),
):
    scope_info = _scope()
    results = (
        queries.search_candidacies(session, q=q, uf=uf, cargo=cargo, limit=50)
        if (q or uf or cargo)
        else []
    )
    ufs = scope_info["ufs"]
    scope_label = (
        f"Eleições {scope_info['election_year']} · "
        + (", ".join(ufs) if ufs else "Brasil")
        + " · "
        + ", ".join(c["nome"].title() for c in scope_info["cargos"])
    )
    template = "_results.html" if request.headers.get("HX-Request") else "index.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "results": results,
            "q": q or "",
            "uf": uf or "",
            "cargo": cargo or "",
            "cargo_options": [(c["nome"], c["nome"].title()) for c in scope_info["cargos"]],
            "scope_label": scope_label,
        },
    )


@app.get("/candidato/{sq_candidato}", response_class=HTMLResponse)
def candidate_page(
    request: Request, sq_candidato: str, session: Session = Depends(get_session)
):
    detail = queries.candidate_detail(session, sq_candidato)
    if detail is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return templates.TemplateResponse(request, "candidate.html", {"d": detail})
