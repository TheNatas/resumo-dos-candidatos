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
collect = typer.Typer(
    help="Coletores idempotentes (TSE · Câmara · Senado · ALESC · Executivo · emendas)",
    no_args_is_help=True,
)
review = typer.Typer(help="Fila de revisão manual do vínculo candidato↔mandato", no_args_is_help=True)
app.add_typer(collect, name="collect")
app.add_typer(review, name="review")


def _run(collector, **kwargs) -> None:
    with session_scope() as session:
        result = collector.run(session, **kwargs)
    typer.echo(str(result))


def _split(raw: Optional[str]) -> Optional[list[str]]:
    """Parse a comma-separated CLI override. None = "use the configured scope";
    an explicit empty string = "no filter" (national / all offices)."""
    if raw is None:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


_UF_OPT = typer.Option(None, "--uf", help="UFs (vírgula). Omitir = escopo do .env; '' = todas.")
_CARGO_OPT = typer.Option(
    None, "--cargo", help="CD_CARGO (vírgula, ex. 3,5,6,7). Omitir = escopo do .env; '' = todos."
)


# ── Scope ─────────────────────────────────────────────────────────────────────
@app.command("scope")
def scope() -> None:
    """Print the effective ingestion scope (election year, UFs, cargos)."""
    from resumo import cargos as cargo_mod

    s = get_settings()
    ufs = ", ".join(s.uf_list) or "TODAS (nacional)"
    codes = sorted(s.cargo_set)
    names = ", ".join(f"{c} {cargo_mod.CARGO_NAMES.get(c, '?')}" for c in codes) or "TODOS"
    typer.echo(f"ano_eleicao : {s.election_year}")
    typer.echo(f"legislatura : {s.id_legislatura}")
    typer.echo(f"ufs         : {ufs}")
    typer.echo(f"cargos      : {names}")


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
    year: Optional[int] = None,
    source: Optional[Path] = typer.Option(None, help="local zip/csv"),
    uf: Optional[str] = _UF_OPT,
    cargo: Optional[str] = _CARGO_OPT,
) -> None:
    from resumo.ingestion.tse.consulta_cand import ConsultaCandCollector

    codes = _split(cargo)
    _run(
        ConsultaCandCollector(),
        year=year,
        source=source,
        ufs=_split(uf),
        cargo_codes=[int(c) for c in codes] if codes is not None else None,
    )


@collect.command("tse-assets")
def tse_assets(
    year: Optional[int] = None,
    source: Optional[Path] = None,
    uf: Optional[str] = _UF_OPT,
) -> None:
    from resumo.ingestion.tse.bem_candidato import BemCandidatoCollector

    _run(BemCandidatoCollector(), year=year, source=source, ufs=_split(uf))


@collect.command("tse-proposta")
def tse_proposta(
    year: Optional[int] = None,
    uf: Optional[str] = typer.Option(None, help="UF única; omitir = todas as UFs do escopo"),
    source: Optional[Path] = None,
) -> None:
    from resumo.ingestion.tse.proposta_governo import PropostaGovernoCollector

    _run(PropostaGovernoCollector(), year=year, uf=uf, source=source)


@collect.command("tse-fotos")
def tse_fotos(
    year: Optional[int] = None,
    uf: Optional[str] = typer.Option(None, help="UF única; omitir = todas as UFs do escopo"),
    source: Optional[Path] = None,
) -> None:
    """Fotos oficiais de registro das candidaturas (pacote por UF do TSE)."""
    from resumo.ingestion.tse.foto_candidato import FotoCandidatoCollector

    _run(FotoCandidatoCollector(), year=year, uf=uf, source=source)


@collect.command("tse-contas")
def tse_contas(
    year: Optional[int] = None,
    source: Optional[Path] = typer.Option(None, help="local zip/csv"),
    uf: Optional[str] = _UF_OPT,
) -> None:
    """Prestação de contas eleitorais (receitas, despesas, pagamentos, doadores).

    Publishes nothing until the first filing window — an empty result is the normal
    outcome before September 2026, not an error.
    """
    from resumo.ingestion.tse.prestacao_contas import PrestacaoContasCollector

    _run(PrestacaoContasCollector(), year=year, source=source, ufs=_split(uf))


# ── Câmara ────────────────────────────────────────────────────────────────────
@collect.command("camara-deputados")
def camara_deputados(
    legislatura: Optional[int] = None,
    limit: Optional[int] = None,
    uf: Optional[str] = _UF_OPT,
) -> None:
    from resumo.ingestion.camara.deputados import DeputadosCollector

    _run(DeputadosCollector(), id_legislatura=legislatura, limit=limit, ufs=_split(uf))


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


@collect.command("camara-presenca")
def camara_presenca(
    anos: Optional[str] = typer.Option(
        None, help="anos (vírgula, ex. 2023,2024). Omitir = os 4 anos da legislatura."
    ),
    legislatura: Optional[int] = None,
    limit: Optional[int] = typer.Option(None, help="máximo de deputados"),
) -> None:
    """Relatório OFICIAL de presença em plenário (dias e sessões, com faltas)."""
    from resumo.ingestion.camara.presenca_plenario import PresencaPlenarioCollector

    years = _split(anos)
    _run(
        PresencaPlenarioCollector(),
        anos=[int(a) for a in years] if years else None,
        id_legislatura=legislatura,
        limit=limit,
    )


# ── Senado ────────────────────────────────────────────────────────────────────
@collect.command("senado-senadores")
def senado_senadores(
    legislatura: Optional[int] = None,
    limit: Optional[int] = None,
    uf: Optional[str] = _UF_OPT,
) -> None:
    from resumo.ingestion.senado.senadores import SenadoresCollector

    _run(SenadoresCollector(), id_legislatura=legislatura, limit=limit, ufs=_split(uf))


@collect.command("senado-votacoes")
def senado_votacoes(
    inicio: str = typer.Option(..., help="YYYY-MM-DD"),
    fim: str = typer.Option(..., help="YYYY-MM-DD"),
    legislatura: Optional[int] = None,
    limit: Optional[int] = None,
) -> None:
    from resumo.ingestion.senado.votacoes import VotacoesCollector

    _run(VotacoesCollector(), data_inicio=inicio, data_fim=fim, id_legislatura=legislatura, limit=limit)


@collect.command("senado-licencas")
def senado_licencas(
    legislatura: Optional[int] = None,
    limit: Optional[int] = None,
) -> None:
    """Licenças e afastamentos formais (dias corridos, com o motivo publicado)."""
    from resumo.ingestion.senado.licencas import LicencasCollector

    _run(LicencasCollector(), id_legislatura=legislatura, limit=limit)


@collect.command("senado-presenca-resumo")
def senado_presenca_resumo() -> None:
    """Consolida a presença derivada das votações nominais (sem rede)."""
    from resumo.ingestion.attendance_summary import AttendanceSummaryCollector

    _run(AttendanceSummaryCollector("senado_votacao_comparecimento"))


@collect.command("senado-proposicoes")
def senado_proposicoes(legislatura: Optional[int] = None, limit: Optional[int] = None) -> None:
    from resumo.ingestion.senado.proposicoes import ProposicoesCollector

    _run(ProposicoesCollector(), id_legislatura=legislatura, limit=limit)


@collect.command("senado-despesas")
def senado_despesas(
    legislatura: Optional[int] = None,
    anos: Optional[str] = typer.Option(None, help="comma-separated, e.g. 2023,2024,2025"),
    limit: Optional[int] = None,
) -> None:
    from resumo.ingestion.senado.despesas import DespesasCollector

    anos_list = [int(a) for a in anos.split(",")] if anos else None
    _run(DespesasCollector(), id_legislatura=legislatura, anos=anos_list, limit=limit)


# ── ALESC (Assembleia Legislativa de SC) ──────────────────────────────────────
@collect.command("alesc-deputados")
def alesc_deputados(
    legislatura: Optional[int] = None,
    limit: Optional[int] = None,
    fallback: bool = typer.Option(
        False, help="usar a lista do e-Legis (sem partido) se o admin-ajax quebrar"
    ),
) -> None:
    from resumo.ingestion.alesc.deputados import DeputadosCollector

    _run(DeputadosCollector(), id_legislatura=legislatura, limit=limit, fallback=fallback)


@collect.command("alesc-despesas")
def alesc_despesas(
    legislatura: Optional[int] = None,
    anos: Optional[str] = typer.Option(None, help="anos (vírgula, ex. 2024,2025,2026)"),
    datasets: Optional[str] = typer.Option(
        None, help="gabinetes-parlamentares,diarias (vírgula)"
    ),
    limit: Optional[int] = None,
) -> None:
    from resumo.ingestion.alesc.despesas import DespesasCollector

    _run(
        DespesasCollector(),
        id_legislatura=legislatura,
        anos=[int(a) for a in anos.split(",")] if anos else None,
        datasets=_split(datasets),
        limit=limit,
    )


@collect.command("alesc-votacoes")
def alesc_votacoes(
    inicio: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    fim: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    legislatura: Optional[int] = None,
    limit: Optional[int] = typer.Option(None, help="máximo de sessões"),
    max_pages: Optional[int] = None,
) -> None:
    """Votações nominais da ALESC — ~96% das matérias são simbólicas (sem posição
    individual), então o volume é baixo por construção."""
    from resumo.ingestion.alesc.votacoes import VotacoesCollector

    _run(
        VotacoesCollector(),
        data_inicio=inicio,
        data_fim=fim,
        id_legislatura=legislatura,
        limit=limit,
        max_pages=max_pages,
    )


@collect.command("alesc-presenca")
def alesc_presenca(
    inicio: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    fim: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    legislatura: Optional[int] = None,
    limit: Optional[int] = typer.Option(None, help="máximo de sessões"),
    max_pages: Optional[int] = None,
) -> None:
    from resumo.ingestion.alesc.presenca import PresencaCollector

    _run(
        PresencaCollector(),
        data_inicio=inicio,
        data_fim=fim,
        id_legislatura=legislatura,
        limit=limit,
        max_pages=max_pages,
    )


@collect.command("alesc-presenca-resumo")
def alesc_presenca_resumo() -> None:
    """Consolida a presença das sessões plenárias já coletadas (sem rede)."""
    from resumo.ingestion.attendance_summary import AttendanceSummaryCollector

    _run(AttendanceSummaryCollector("alesc_sessao_presenca"))


@collect.command("alesc-proposicoes")
def alesc_proposicoes(
    legislatura: Optional[int] = None,
    anos: Optional[str] = typer.Option(None, help="anos (vírgula) — particiona o crawl"),
    tracks: Optional[str] = typer.Option(
        None, help="processo-legislativo,atividade-parlamentar (vírgula)"
    ),
    limit: Optional[int] = typer.Option(None, help="máximo de deputados"),
) -> None:
    from resumo.ingestion.alesc.proposicoes import ProposicoesCollector

    _run(
        ProposicoesCollector(),
        id_legislatura=legislatura,
        anos=[int(a) for a in anos.split(",")] if anos else None,
        tracks=_split(tracks),
        limit=limit,
    )


# ── Executivo (governadores em exercício) ─────────────────────────────────────
@collect.command("executivo-governadores")
def executivo_governadores(
    year: Optional[int] = typer.Option(
        None, help="Eleição que DEU posse (2022 para o ciclo 2026). Omitir = derivar."
    ),
    uf: Optional[str] = _UF_OPT,
) -> None:
    """Mandatos dos governadores em exercício, derivados do resultado do TSE.

    Sem rede: lê as candidaturas ELEITO que `collect tse-candidates` já trouxe para a
    eleição anterior. Rode-o antes, ou este comando devolve `empty`.
    """
    from resumo.ingestion.executivo.governadores import GovernadoresCollector

    _run(GovernadoresCollector(), year=year, ufs=_split(uf))


@collect.command("executivo-atos")
def executivo_atos(
    year: Optional[int] = typer.Option(
        None, help="Eleição que DEU posse (2022 para o ciclo 2026). Omitir = derivar."
    ),
    max_pages: int = typer.Option(120, help="Guarda contra laço infinito na paginação."),
) -> None:
    """Projetos de iniciativa do Executivo e mensagens de veto (e-Legis, só SC).

    Rode DEPOIS de `collect executivo-governadores`: os atos são atribuídos ao mandato
    cuja janela contém a data de entrada, então precisa do mandato já em base.
    """
    from resumo.ingestion.executivo.atos import AtosCollector

    _run(AtosCollector(), year=year, max_pages=max_pages)


# ── Emendas parlamentares ─────────────────────────────────────────────────────
@collect.command("emendas")
def emendas(
    source: Optional[Path] = typer.Option(None, help="local zip/csv"),
    uf: Optional[str] = _UF_OPT,
    anos: Optional[str] = typer.Option(None, help="anos (vírgula); omitir = todos"),
) -> None:
    """Emendas parlamentares (arquivo em lote da CGU, sem chave de API)."""
    from resumo.ingestion.emendas.emendas_parlamentares import EmendasParlamentaresCollector

    _run(
        EmendasParlamentaresCollector(),
        source=source,
        ufs=_split(uf),
        anos=[int(a) for a in anos.split(",")] if anos else None,
    )


# ── Resolution ────────────────────────────────────────────────────────────────
@app.command("link-emendas-authors")
def link_emendas_authors(
    anos: Optional[str] = typer.Option(None, help="anos (vírgula); omitir = todos"),
) -> None:
    """Materializa o vínculo código SIOP↔mandato e propaga para as emendas.

    Rode DEPOIS de `collect camara-deputados` / `collect senado-senadores`: a ponte
    casa pelo nome parlamentar dentro da UF, então precisa dos mandatos já em base.
    """
    from resumo.ingestion.emendas.author_bridge import resolve_authors

    with session_scope() as session:
        result = resolve_authors(
            session, anos=[int(a) for a in anos.split(",")] if anos else None
        )
    typer.echo(str(result))


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


@review.command("validate")
def review_validate(
    file: Path = typer.Option(
        Path("review-decisions.yml"), "--file", "-f", help="Arquivo de decisões."
    ),
) -> None:
    """Valida o arquivo de decisões sem tocar no banco (roda em CI a cada push)."""
    from resumo.review import DecisionFileError, load_decisions

    try:
        decisions = load_decisions(file)
    except DecisionFileError as exc:
        typer.echo(f"{file}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{file}: {len(decisions)} decisão(ões), formato válido")


@review.command("apply")
def review_apply(
    file: Path = typer.Option(
        Path("review-decisions.yml"), "--file", "-f", help="Arquivo de decisões."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Falha se alguma decisão não puder ser aplicada."
    ),
) -> None:
    """Aplica as decisões versionadas à fila de revisão (rode antes de `resolve`)."""
    from resumo.review import DecisionFileError, apply_decisions, load_decisions

    try:
        decisions = load_decisions(file)
    except DecisionFileError as exc:
        raise typer.BadParameter(str(exc)) from exc

    with session_scope() as session:
        result = apply_decisions(session, decisions)

    typer.echo(str(result))
    for label in result.missing_mandate:
        typer.echo(f"  sem mandato em base: {label}")
    for label in result.missing_queue:
        typer.echo(f"  par não está na fila: {label}")
    if strict and result.skipped:
        raise typer.Exit(code=1)


@review.command("export")
def review_export(
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Escreve em arquivo."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Máximo de pendências."),
) -> None:
    """Exporta a fila pendente no formato de `review-decisions.yml`."""
    from resumo.review import export_pending

    with session_scope() as session:
        text = export_pending(session, limit=limit)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"{out} escrito")
    else:
        typer.echo(text)


# ── Serve ─────────────────────────────────────────────────────────────────────
@app.command("render")
def render(
    out: Path = typer.Option(Path("_site"), "--out", help="Diretório de saída."),
    base_url: Optional[str] = typer.Option(
        None, "--base-url", help="Prefixo de URL. Omitir = RESUMO_SITE_BASE_URL."
    ),
    site_url: Optional[str] = typer.Option(
        None, "--site-url", help="URL absoluta do site; habilita sitemap.xml."
    ),
    clean: bool = typer.Option(True, help="Apaga o diretório de saída antes de gerar."),
) -> None:
    """Renderiza o site estático (mesmos templates e queries do app vivo)."""
    from resumo.render import render_site

    with session_scope() as session:
        result = render_site(
            session, out=out, base_url=base_url, site_url=site_url, clean=clean
        )
    typer.echo(str(result))


@app.command("janela")
def janela(
    house: str = typer.Option(..., "--house", help="CAMARA | SENADO | ASSEMBLEIA."),
    dias: int = typer.Option(30, "--dias", help="Dias de sobreposição."),
    piso: Optional[str] = typer.Option(None, "--piso", help="Data mínima (ISO)."),
) -> None:
    """Imprime de onde recomeçar a coleta de votações (janela incremental)."""
    from resumo.db.models import House
    from resumo.window import incremental_start

    try:
        casa = House(house.strip().upper())
    except ValueError as exc:
        raise typer.BadParameter(f"house inválida: {house!r}") from exc

    chao = piso or f"{get_settings().election_year}-01-01"
    with session_scope() as session:
        typer.echo(incremental_start(session, casa, floor=chao, overlap_days=dias))


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    typer.echo(f"election_year={get_settings().election_year}  →  http://{host}:{port}")
    uvicorn.run("resumo.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
