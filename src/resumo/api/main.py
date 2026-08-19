"""FastAPI app: JSON API (/api/...) + server-rendered public front (Jinja + htmx)."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from resumo import cargos
from resumo.api import queries
from resumo.api.deps import get_session
from resumo.api.routers import candidates
from resumo.config import get_settings
from resumo.db.models import CandidatePhoto, GovernmentProposal

_WEB = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB / "templates"))
# Globals rather than per-view context: every page needs them, and the static
# renderer reuses these same templates with a different base_url.
templates.env.globals["base_url"] = get_settings().site_base_url
templates.env.globals["election_year"] = get_settings().election_year

# The filter is a <select>, so "no choice" arrives as an empty string — a
# `bool | None` query param would 422 on it. Parsed here instead, which also keeps
# the URL readable ("?reeleicao=sim") for a link someone may share.
_REELEICAO = {"sim": True, "nao": False}

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


def _scope_label(scope: dict) -> str:
    ufs = scope["ufs"]
    return (
        f"Eleições {scope['election_year']} · "
        + (", ".join(ufs) if ufs else "Brasil")
        + " · "
        + ", ".join(c["nome"].title() for c in scope["cargos"])
    )


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
    cargo: str | None = None,
    partido: str | None = None,
    reeleicao: str | None = None,
    session: Session = Depends(get_session),
):
    scope_info = _scope()
    results = (
        queries.search_candidacies(
            session,
            q=q,
            cargo=cargo,
            partido=partido,
            reeleicao=_REELEICAO.get((reeleicao or "").lower()),
            # Pinned to the configured year: the same database also holds the
            # historical validation set (2022), and an unscoped search lists those
            # rows — with their "ELEITO" badges — under an "Eleições 2026" header.
            year=scope_info["election_year"],
            limit=50,
        )
        if (q or cargo or partido or reeleicao)
        else []
    )
    scope_label = _scope_label(scope_info)
    template = "_results.html" if request.headers.get("HX-Request") else "index.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "results": results,
            "q": q or "",
            "cargo": cargo or "",
            "partido": partido or "",
            "reeleicao": reeleicao or "",
            "cargo_options": [(c["nome"], c["nome"].title()) for c in scope_info["cargos"]],
            "partido_options": queries.partidos_in_scope(
                session,
                year=scope_info["election_year"],
                ufs=scope_info["ufs"],
                cargo_codes=[c["cd_cargo"] for c in scope_info["cargos"]],
            ),
            "scope_label": scope_label,
        },
    )


@app.get("/sobre", response_class=HTMLResponse)
def about(request: Request):
    """As ressalvas de fonte que qualificam todo número do site. Uma página, não uma
    nota de rodapé: quem lê "12 votos" precisa poder descobrir num clique que 96% das
    votações daquela Casa são simbólicas."""
    return templates.TemplateResponse(
        request, "sobre.html", {"scope_label": _scope_label(_scope())}
    )


@app.get("/candidato/{sq_candidato}", response_class=HTMLResponse)
def candidate_page(
    request: Request, sq_candidato: str, session: Session = Depends(get_session)
):
    detail = queries.candidate_detail(session, sq_candidato)
    if detail is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return templates.TemplateResponse(request, "candidate.html", {"d": detail})


@app.get("/foto/{sq_candidato}.jpg")
def candidate_photo(sq_candidato: str, session: Session = Depends(get_session)):
    """Serve a candidacy's official registration photo.

    The URL says `.jpg` because that is what TSE ships and what the page asks for;
    the response's media type comes from the stored row, so a bundle that ever
    contains a PNG is served correctly instead of being labelled a lie.
    """
    photo = session.get(CandidatePhoto, sq_candidato)
    if photo is None or not photo.storage_path:
        raise HTTPException(status_code=404, detail="foto não encontrada")
    path = Path(photo.storage_path).resolve()
    root = get_settings().storage_path().resolve()
    # Same confinement as the proposta route: the path is a filesystem path read out
    # of a database row, and no row may turn this into an arbitrary-file read.
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="arquivo indisponível")
    return FileResponse(path, media_type=photo.media_type)


@app.get("/proposta/{proposal_id}.pdf")
def proposta_pdf(proposal_id: uuid.UUID, session: Session = Depends(get_session)):
    """Serve a collected proposta de governo. Kept as its own route (not StaticFiles)
    so the URL is the stable proposal id rather than the storage layout."""
    proposal = session.get(GovernmentProposal, proposal_id)
    if proposal is None or not proposal.storage_path:
        raise HTTPException(status_code=404, detail="proposta não encontrada")
    path = Path(proposal.storage_path).resolve()
    root = get_settings().storage_path().resolve()
    # The path was written by our own collector, but it is still a filesystem path
    # read out of a database row: confine it to the storage root so no row can ever
    # turn this route into an arbitrary-file read.
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="arquivo indisponível")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=proposal.original_filename or f"proposta-{proposal_id}.pdf",
    )
