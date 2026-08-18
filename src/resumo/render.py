"""Static site renderer.

The public surface is read-only, so the database does not have to sit in the request
path: the same queries and the same Jinja templates the live app uses run once at
build time and emit plain files. Postgres stays the store — it moves from
"once per visitor" to "once per build".

What comes out is self-contained (no CDN, no API calls) and is published as-is:

    _site/
      index.html                    todas as candidaturas do escopo + filtro em JS
      sobre/index.html              fontes, ressalvas e metodologia
      candidato/<sq>/index.html     mesma URL que o app vivo serve
      api/scope.json                o que este deploy cobre
      api/candidates.json           índice de busca
      api/candidates/<sq>.json      mesma resposta do endpoint JSON
      proposta/<id>.pdf             as propostas de governo coletadas
      static/                       CSS
      404.html · robots.txt · sitemap.xml · .nojekyll
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from resumo import cargos
from resumo.api import queries
from resumo.config import get_settings

_WEB = Path(__file__).resolve().parent / "web"


@dataclass
class RenderResult:
    pages: int
    proposals: int
    out: Path

    def __str__(self) -> str:  # mirrors the collectors' one-line CLI output
        return f"render: {self.pages} fichas, {self.proposals} propostas → {self.out}"


def _now_brt() -> str:
    """Readers need to know how fresh the page is; a data site without an extraction
    timestamp asks to be trusted on nothing."""
    try:
        from zoneinfo import ZoneInfo

        stamp = dt.datetime.now(ZoneInfo("America/Sao_Paulo"))
        return stamp.strftime("%d/%m/%Y às %H:%M (BRT)")
    except Exception:  # tzdata absent (slim containers) — UTC is still truthful
        return dt.datetime.now(dt.UTC).strftime("%d/%m/%Y às %H:%M (UTC)")


def _environment(*, base_url: str, election_year: int, generated_at: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_WEB / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    # The live app sets these on its own Jinja2Templates env; the templates are shared
    # and must not know which of the two is rendering them.
    env.globals.update(
        base_url=base_url,
        election_year=election_year,
        static_mode=True,
        generated_at=generated_at,
    )
    return env


def _scope_info() -> dict:
    s = get_settings()
    codes = sorted(s.cargo_set) or sorted(cargos.CARGO_NAMES)
    return {
        "election_year": s.election_year,
        "ufs": list(s.uf_list),
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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload) -> None:
    # sort_keys + ensure_ascii=False: the build runs daily, and a stable, readable
    # serialization keeps a diff of two days' output meaningful.
    _write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _prepare_out(out: Path, clean: bool) -> None:
    """Refuse to delete a directory that is not ours. `--out` is a path typed by a
    human (or interpolated in CI); `.nojekyll` is the marker that says we wrote it."""
    if out.exists() and clean:
        if any(out.iterdir()) and not (out / ".nojekyll").exists():
            raise ValueError(
                f"{out} não está vazio e não parece um site renderizado "
                "(sem .nojekyll). Recusando apagar — escolha outro --out."
            )
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / ".nojekyll").touch()  # sem isso o Pages roda Jekyll e ignora caminhos com "_"


def _copy_proposals(detail: dict, out: Path) -> int:
    """Copy each collected PDF to its public id-based path and rewrite the payload's
    URL. A proposta that was downloaded but never linked is a feature that does not
    exist as far as the reader is concerned."""
    copied = 0
    root = get_settings().storage_path().resolve()
    for proposal in detail.get("proposals") or []:
        source = proposal.pop("_storage_path", None)
        if not source:
            continue
        path = Path(source).resolve()
        # Same confinement rule as the live route: the path comes from a database row.
        if root not in path.parents or not path.is_file():
            continue
        target = out / "proposta" / f"{proposal['id']}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied += 1
    return copied


def render_site(
    session: Session,
    *,
    out: Path,
    base_url: str | None = None,
    site_url: str | None = None,
    clean: bool = True,
) -> RenderResult:
    settings = get_settings()
    base_url = (settings.site_base_url if base_url is None else base_url).rstrip("/")
    scope = _scope_info()
    generated_at = _now_brt()
    env = _environment(
        base_url=base_url, election_year=scope["election_year"], generated_at=generated_at
    )

    out = out.resolve()
    _prepare_out(out, clean)

    # The set of pages is an explicit scoped query, not whatever a search box asked
    # for: the same database also holds the historical validation set.
    summaries = queries.candidacies_in_scope(
        session,
        year=scope["election_year"],
        ufs=scope["ufs"],
        cargo_codes=sorted(settings.cargo_set),
    )

    proposals = 0
    for summary in summaries:
        sq = summary["sq_candidato"]
        detail = queries.candidate_detail(session, sq, include_storage_path=True)
        if detail is None:  # pragma: no cover — the summary came from the same table
            continue
        proposals += _copy_proposals(detail, out)
        _write(
            out / "candidato" / sq / "index.html",
            env.get_template("candidate.html").render(d=detail),
        )
        _write_json(out / "api" / "candidates" / f"{sq}.json", detail)

    _write(
        out / "index.html",
        env.get_template("index_static.html").render(
            results=summaries,
            scope_label=_scope_label(scope),
            cargo_options=[(c["nome"], c["nome"].title()) for c in scope["cargos"]],
            total=len(summaries),
        ),
    )
    _write(
        out / "sobre" / "index.html",
        env.get_template("sobre.html").render(scope_label=_scope_label(scope)),
    )
    _write(out / "404.html", env.get_template("not_found.html").render())
    _write_json(out / "api" / "scope.json", scope)
    _write_json(out / "api" / "candidates.json", summaries)

    shutil.copytree(_WEB / "static", out / "static", dirs_exist_ok=True)
    _write_robots(out, site_url)
    if site_url:
        _write_sitemap(out, site_url.rstrip("/"), summaries, generated_at)

    return RenderResult(pages=len(summaries), proposals=proposals, out=out)


def _write_robots(out: Path, site_url: str | None) -> None:
    lines = ["User-agent: *", "Allow: /"]
    if site_url:
        lines.append(f"Sitemap: {site_url.rstrip('/')}/sitemap.xml")
    _write(out / "robots.txt", "\n".join(lines) + "\n")


def _write_sitemap(out: Path, site_url: str, summaries: list[dict], generated_at: str) -> None:
    today = dt.date.today().isoformat()
    urls = (
        [f"{site_url}/", f"{site_url}/sobre/"]
        + [f"{site_url}/candidato/{s['sq_candidato']}/" for s in summaries]
    )
    body = "\n".join(
        f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>" for u in urls
    )
    _write(
        out / "sitemap.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n</urlset>\n",
    )
