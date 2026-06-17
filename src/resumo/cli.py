"""`resumo` CLI — every collector is an idempotent command (cron-friendly)."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

import typer

from resumo.config import get_settings
from resumo.db.session import session_scope

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = typer.Typer(help="Plataforma de Transparência Eleitoral 2026", no_args_is_help=True)
collect = typer.Typer(help="Coletores idempotentes (TSE + Câmara)", no_args_is_help=True)
review = typer.Typer(help="Fila de revisão manual do vínculo candidato↔mandato", no_args_is_help=True)
app.add_typer(collect, name="collect")
app.add_typer(review, name="review")


def _run(collector, **kwargs) -> None:
    with session_scope() as session:
        result = collector.run(session, **kwargs)
    typer.echo(str(result))


# ── DB ────────────────────────────────────────────────────────────────────────
@app.command("db-upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations (run from repo root)."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")


# ── TSE ───────────────────────────────────────────────────────────────────────
@collect.command("tse-candidates")
def tse_candidates(
    year: Optional[int] = None, source: Optional[Path] = typer.Option(None, help="local zip/csv")
) -> None:
    from resumo.ingestion.tse.consulta_cand import ConsultaCandCollector

    _run(ConsultaCandCollector(), year=year, source=source)


@collect.command("tse-assets")
def tse_assets(year: Optional[int] = None, source: Optional[Path] = None) -> None:
    from resumo.ingestion.tse.bem_candidato import BemCandidatoCollector

    _run(BemCandidatoCollector(), year=year, source=source)


@collect.command("tse-proposta")
def tse_proposta(
    year: Optional[int] = None,
    uf: Optional[str] = typer.Option(None, help="UF (required unless --source)"),
    source: Optional[Path] = None,
) -> None:
    from resumo.ingestion.tse.proposta_governo import PropostaGovernoCollector

    _run(PropostaGovernoCollector(), year=year, uf=uf, source=source)


# ── Câmara ────────────────────────────────────────────────────────────────────
@collect.command("camara-deputados")
def camara_deputados(legislatura: Optional[int] = None, limit: Optional[int] = None) -> None:
    from resumo.ingestion.camara.deputados import DeputadosCollector

    _run(DeputadosCollector(), id_legislatura=legislatura, limit=limit)


@collect.command("camara-despesas")
def camara_despesas(
    legislatura: Optional[int] = None,
    anos: Optional[str] = typer.Option(None, help="comma-separated, e.g. 2023,2024,2025"),
    limit: Optional[int] = None,
) -> None:
    from resumo.ingestion.camara.despesas import DespesasCollector

    anos_list = [int(a) for a in anos.split(",")] if anos else None
    _run(DespesasCollector(), id_legislatura=legislatura, anos=anos_list, limit=limit)


@collect.command("camara-proposicoes")
def camara_proposicoes(legislatura: Optional[int] = None, limit: Optional[int] = None) -> None:
    from resumo.ingestion.camara.proposicoes import ProposicoesCollector

    _run(ProposicoesCollector(), id_legislatura=legislatura, limit=limit)


@collect.command("camara-votacoes")
def camara_votacoes(
    inicio: str = typer.Option(..., help="YYYY-MM-DD"),
    fim: str = typer.Option(..., help="YYYY-MM-DD"),
    legislatura: Optional[int] = None,
    limit: Optional[int] = None,
) -> None:
    from resumo.ingestion.camara.votacoes import VotacoesCollector

    _run(VotacoesCollector(), data_inicio=inicio, data_fim=fim, id_legislatura=legislatura, limit=limit)


@collect.command("camara-eventos")
def camara_eventos(
    inicio: str = typer.Option(..., help="YYYY-MM-DD"),
    fim: str = typer.Option(..., help="YYYY-MM-DD"),
    legislatura: Optional[int] = None,
    limit: Optional[int] = None,
) -> None:
    from resumo.ingestion.camara.eventos import EventosCollector

    _run(EventosCollector(), data_inicio=inicio, data_fim=fim, id_legislatura=legislatura, limit=limit)


# ── Resolution ────────────────────────────────────────────────────────────────
@app.command("resolve")
def resolve(year: Optional[int] = None) -> None:
    """Materialize the candidate↔mandate links (and review queue)."""
    from resumo.resolution.pipeline import resolve as run_resolve

    with session_scope() as session:
        result = run_resolve(session, year=year)
    typer.echo(str(result))


# ── Review queue (manual override) ────────────────────────────────────────────
@review.command("list")
def review_list(limit: int = 20) -> None:
    from sqlalchemy import select

    from resumo.db.models import ReviewQueue, ReviewStatus

    with session_scope() as session:
        rows = session.execute(
            select(ReviewQueue).where(ReviewQueue.status == ReviewStatus.pending).limit(limit)
        ).scalars()
        for r in rows:
            typer.echo(f"{r.id}  {r.suggested_score:.2f}  {r.reason}  {r.candidate_snapshot}")


@review.command("decide")
def review_decide(
    review_id: str,
    decision: str = typer.Argument(..., help="match | no_match | uncertain"),
    by: str = "cli",
) -> None:
    from resumo.db.models import ReviewQueue, ReviewStatus

    with session_scope() as session:
        row = session.get(ReviewQueue, review_id)
        if row is None:
            raise typer.BadParameter("review id não encontrado")
        row.status = ReviewStatus(decision)
        row.decided_by = by
        row.decided_at = dt.datetime.now(dt.UTC)
    typer.echo(f"review {review_id} -> {decision} (re-run `resumo resolve` to apply)")


# ── Serve ─────────────────────────────────────────────────────────────────────
@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    typer.echo(f"election_year={get_settings().election_year}  →  http://{host}:{port}")
    uvicorn.run("resumo.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
