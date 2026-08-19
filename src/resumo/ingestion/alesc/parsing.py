"""HTML parsing for ALESC — stdlib only.

ALESC has no API, so every legislative source is HTML. This module deliberately uses
``html.parser`` from the standard library instead of adding ``selectolax`` /
``beautifulsoup4``: the dependency surface of this project is a feature, and the five
ALESC documents we read are small and structurally simple.

The mini-DOM itself lives in :mod:`resumo.ingestion.html` (the Câmara presence
report needs the same tree) and is re-exported here, so every ``parse_*`` function
below reads exactly as it did before. Selecting on **CSS classes** — never on label
text — is the rule: ALESC renders vote positions as bootstrap badges
(``text-bg-success`` = Sim, ``text-bg-danger`` = Não) and the visible label is the
part most likely to be reworded.

Source of truth for each shape (verified live 2026-08-18):

* roster fragment  -> ``{alesc_site_base}/wp-admin/admin-ajax.php?action=alm_get_posts``
* session index    -> ``{alesc_elegis_base}/sessoes-plenarias?page=N``
* ordem do dia     -> ``{alesc_elegis_base}/sessoes-plenarias/{hash}/ordem-do-dia``
* extrato          -> ``{alesc_elegis_base}/extrato-votacao/{hash}``
* presença         -> ``{alesc_elegis_base}/sessoes-plenarias/{hash}/presenca``
* proposições      -> ``{alesc_elegis_base}/proposicoes/{track}?iniciativa=&ano=``
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

from resumo.ingestion.html import Node, find, find_all, iter_elements, parse_html, text_of
from resumo.util import clean, parse_date, parse_int

__all__ = [
    "AlescParseError", "ExtratoVotacao", "Node", "OrdemItem", "PresencaEntry",
    "PropositionCard", "RosterEntry", "SessionRef", "find", "find_all",
    "is_current_member_label", "is_electoral_blackout", "next_page_url", "parse_extrato_votacao",
    "parse_html", "parse_iniciativa_options", "parse_ordem_do_dia", "parse_presenca",
    "parse_proposition_cards", "parse_result_total", "parse_roster_html",
    "parse_roster_payload", "parse_session_index", "split_codigo", "text_of",
]

logger = logging.getLogger("resumo.ingestion.alesc")


class AlescParseError(RuntimeError):
    """Upstream HTML/JSON did not have the shape this module knows how to read.

    Raised (never a bare AttributeError/KeyError) so collectors can turn upstream
    drift into a clear CollectorResult instead of a stack trace.
    """


# ── Shared regexes ───────────────────────────────────────────────────────────
_WS = re.compile(r"\s+")
_SLUG_RE = re.compile(r"/deputado/([^/?#]+)")
_SESSION_RE = re.compile(r"/sessoes-plenarias/([A-Za-z0-9]+)(?:/([a-z-]+))?")
_PROPOSICAO_RE = re.compile(r"/proposicoes/([A-Za-z0-9]+)")
_EXTRATO_RE = re.compile(r"/extrato-votacao/([A-Za-z0-9]+)")
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_TOTAL_RE = re.compile(r"Exibindo\s+[\d.]+\s*-\s*[\d.]+\s+de\s+([\d.]+)")
# "PL./0578/2024", "RQS/2862/2026", "PLC/0017/2024" -> (sigla, numero, ano)
_CODIGO_RE = re.compile(r"^([A-ZÇÃÕ.]{2,10})\s*/\s*(\d{1,6})\s*/\s*(\d{4})$")

# The two badge classes ALESC actually uses for a nominal position. Anything else is
# unknown vocabulary: fall back to the visible label rather than guessing or crashing.
# Measured across 44 extratos: only Sim/Não were ever observed — no abstention.
BADGE_VOTE = {"text-bg-success": "Sim", "text-bg-danger": "Não"}

NOMINAL = "Votação nominal"
SIMBOLICA = "Votação simbólica"


# ── Roster (WordPress admin-ajax) ────────────────────────────────────────────
@dataclass(frozen=True)
class RosterEntry:
    """One deputy card. `slug` is ALESC's only stable member identifier."""

    slug: str
    nome: str
    sigla_partido: str | None


def parse_roster_payload(payload: object) -> tuple[list[RosterEntry], int | None]:
    """Read the ``alm_get_posts`` envelope: ``{"html": ..., "meta": {...}}``.

    🚨 This is an **undocumented WordPress plugin endpoint** (Ajax Load More) — the
    single most fragile source in the ALESC set. Any shape drift raises
    :class:`AlescParseError` with a message naming what was missing.
    """
    if not isinstance(payload, dict):
        raise AlescParseError(
            f"admin-ajax returned {type(payload).__name__}, expected a JSON object "
            '{"html": ..., "meta": ...}'
        )
    markup = payload.get("html")
    if not isinstance(markup, str) or not markup.strip():
        raise AlescParseError(
            "admin-ajax payload has no usable 'html' key "
            f"(keys={sorted(payload)!r}) — the alm_get_posts contract changed"
        )
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    total = parse_int(str(meta.get("totalposts"))) if meta.get("totalposts") is not None else None
    return parse_roster_html(markup), total


def parse_roster_html(markup: str) -> list[RosterEntry]:
    root = parse_html(markup)
    entries: list[RosterEntry] = []
    seen: set[str] = set()
    for card in find_all(root, "article", cls="lab-card-team"):
        slug = None
        for link in find_all(card, "a", attr="href"):
            match = _SLUG_RE.search(link.get("href") or "")
            if match:
                slug = match.group(1).strip("/")
                break
        if not slug or slug in seen:
            continue
        nome = text_of(find(card, "h3", cls="lab-title-news"))
        if not nome:
            # The <img alt> repeats the display name; use it before giving up.
            img = find(card, "img", attr="alt")
            nome = (img.get("alt") or "").strip() if img else ""
        partido = text_of(find(card, "span", cls="lab-button")) or None
        seen.add(slug)
        entries.append(RosterEntry(slug=slug, nome=nome or slug.replace("-", " ").title(),
                                   sigla_partido=clean(partido)))
    return entries


def parse_iniciativa_options(markup: str) -> list[tuple[str, str]]:
    """Fallback roster: the ``iniciativa`` <select> on the e-Legis search pages.

    Yields ``(slug, label)`` for every option. Current-legislature members are the
    ones whose label is prefixed ``Deputado``/``Deputada``; historical members appear
    without the prefix. No party is exposed here.
    """
    root = parse_html(markup)
    select = None
    for node in find_all(root, "select"):
        if node.get("name") == "iniciativa":
            select = node
            break
    if select is None:
        raise AlescParseError("no <select name='iniciativa'> on the e-Legis search page")
    out = []
    for option in find_all(select, "option"):
        value = (option.get("value") or "").strip()
        if value:
            out.append((value, text_of(option)))
    return out


def is_current_member_label(label: str) -> bool:
    return label.strip().lower().startswith(("deputado", "deputada"))


# ── Session index ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SessionRef:
    session_hash: str
    titulo: str | None
    data: dt.date | None
    sections: frozenset[str]  # only the sections whose link is NOT `disabled`

    def has(self, section: str) -> bool:
        return section in self.sections


def parse_session_index(markup: str) -> list[SessionRef]:
    root = parse_html(markup)
    sessions: list[SessionRef] = []
    seen: set[str] = set()
    for card in find_all(root, "div", cls={"card", "card-alesc"}):
        session_hash = None
        sections: set[str] = set()
        for link in find_all(card, "a", attr="href"):
            match = _SESSION_RE.search(link.get("href") or "")
            # Require the /{hash}/{section} shape: the sidebar links /sessoes-plenarias/atas
            # and /sessoes-plenarias/pronunciamentos, which are pages, not sessions.
            if not match or not match.group(2):
                continue
            session_hash = session_hash or match.group(1)
            if "disabled" not in link.classes:
                sections.add(match.group(2))
        if not session_hash or session_hash in seen:
            continue
        seen.add(session_hash)
        titulo = text_of(find(card, "h4")) or None
        date_match = _DATE_RE.search(text_of(card))
        sessions.append(
            SessionRef(
                session_hash=session_hash,
                titulo=titulo,
                data=parse_date(date_match.group(1)) if date_match else None,
                sections=frozenset(sections),
            )
        )
    return sessions


def next_page_url(markup: str) -> str | None:
    """The pagination ``rel=next`` href, mirroring the Câmara client's link-following."""
    for link in find_all(parse_html(markup), "a", attr="rel"):
        if link.get("rel") == "next" and link.get("href"):
            return link.get("href")
    return None


def parse_result_total(markup: str) -> int | None:
    """``Exibindo 1 - 10 de 610`` -> 610."""
    match = _TOTAL_RE.search(_WS.sub(" ", markup))
    return parse_int(match.group(1).replace(".", "")) if match else None


# ── Ordem do dia ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrdemItem:
    codigo: str | None  # "PLC/0017/2024"
    proposicao_hash: str | None
    resultado: str | None  # Aprovado / Rejeitado / ...
    tipo_votacao: str | None  # "Votação nominal" | "Votação simbólica"
    extrato_hash: str | None
    ementa: str | None

    @property
    def is_nominal(self) -> bool:
        """Only nominal votings record an individual position.

        🚨 ~96% of ALESC deliberations are *votação simbólica*, which by institutional
        design records **no individual position at all** — measured 44 nominal out of
        1,218 items across 120 sessions (3.6%). There is nothing to synthesize for the
        other 96%: a symbolic vote MUST produce zero Vote rows.
        """
        return bool(self.extrato_hash) and self.tipo_votacao != SIMBOLICA


def parse_ordem_do_dia(markup: str) -> list[OrdemItem]:
    root = parse_html(markup)
    items: list[OrdemItem] = []
    for block in find_all(root, "div", cls={"border-bottom", "mb-3"}):
        link = next(
            (
                a
                for a in find_all(block, "a", attr="href")
                if _PROPOSICAO_RE.search(a.get("href") or "")
            ),
            None,
        )
        if link is None:
            continue
        match = _PROPOSICAO_RE.search(link.get("href") or "")
        resultado = tipo = None
        for badge in find_all(block, "span", cls="badge"):
            label = text_of(badge)
            if label.lower().startswith("votação"):
                tipo = label
            elif label:
                resultado = resultado or label
        extrato = next(
            (
                m.group(1)
                for node in find_all(block, attr="hx-get")
                if (m := _EXTRATO_RE.search(node.get("hx-get") or ""))
            ),
            None,
        )
        items.append(
            OrdemItem(
                codigo=text_of(link) or None,
                proposicao_hash=match.group(1) if match else None,
                resultado=resultado,
                tipo_votacao=tipo,
                extrato_hash=extrato,
                ementa=text_of(find(block, "p", cls="fst-italic")) or None,
            )
        )
    return items


# ── Extrato de votação (htmx fragment) ───────────────────────────────────────
@dataclass(frozen=True)
class ExtratoVotacao:
    codigo: str | None
    situacao: str | None
    votos: tuple[tuple[str, str | None], ...]  # (nome exibido, tipo_voto)


def parse_extrato_votacao(markup: str) -> ExtratoVotacao:
    """Read individual positions off the badge **CSS class**, not the label text.

    Unknown badge classes fall back to the visible label and are logged — no
    abstention badge has ever been observed, so a third class is drift, not an error.
    """
    root = parse_html(markup)
    votos: list[tuple[str, str | None]] = []
    for wrap in find_all(root, "div", cls={"d-flex", "justify-content-between"}):
        badge = next((n for n in iter_elements(wrap) if "badge" in n.classes), None)
        if badge is None:
            continue
        nome = text_of(wrap, exclude=badge)
        if not nome:
            continue
        known = [c for c in badge.classes if c in BADGE_VOTE]
        if known:
            tipo = BADGE_VOTE[known[0]]
        else:
            tipo = clean(text_of(badge))
            logger.warning(
                "ALESC extrato: unknown vote badge %s (label=%r) for %r — kept the label",
                sorted(badge.classes), text_of(badge), nome,
            )
        votos.append((nome, tipo))
    return ExtratoVotacao(
        codigo=text_of(find(root, "h4")) or None,
        situacao=text_of(find(root, "h5")) or None,
        votos=tuple(votos),
    )


# ── Presença ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PresencaEntry:
    nome: str
    presente: bool
    justificativa: str | None


def parse_presenca(markup: str) -> list[PresencaEntry]:
    """``<table class="table table-hover">`` with one ``<tr>`` per deputy.

    Observed values: ``Presente`` and ``Ausência justificada``. Anything that is not
    "Presente" is recorded as an absence and keeps its raw label as the justificativa,
    so an unseen third status is preserved rather than silently coerced.
    """
    root = parse_html(markup)
    table = find(root, "table", cls="table-hover")
    if table is None:
        return []
    entries: list[PresencaEntry] = []
    for row in find_all(table, "tr"):
        cells = [text_of(td) for td in find_all(row, "td")]
        if len(cells) < 2 or not cells[0]:
            continue
        nome, status = cells[0], cells[1]
        presente = status.strip().lower().startswith("presente")
        entries.append(
            PresencaEntry(
                nome=nome,
                presente=presente,
                justificativa=None if presente else (clean(status) or None),
            )
        )
    return entries


# ── Proposições ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PropositionCard:
    proposicao_hash: str
    codigo: str | None
    ementa: str | None
    data_entrada: dt.date | None
    situacao: str | None
    autoria: tuple[str, ...]


def split_codigo(codigo: str | None) -> tuple[str | None, int | None, int | None]:
    """``PL./0534/2026`` -> ``("PL.", 534, 2026)``; unparseable -> ``(codigo, None, None)``."""
    value = clean(codigo)
    if value is None:
        return None, None, None
    match = _CODIGO_RE.match(value.replace(" ", ""))
    if not match:
        return value[:16], None, None
    return match.group(1), parse_int(match.group(2)), parse_int(match.group(3))


def parse_proposition_cards(markup: str) -> list[PropositionCard]:
    root = parse_html(markup)
    cards: list[PropositionCard] = []
    seen: set[str] = set()
    for card in find_all(root, "div", cls={"card", "card-alesc"}):
        title = find(card, "h4", cls="card-title")
        link = find(title, "a", attr="href") if title is not None else None
        match = _PROPOSICAO_RE.search(link.get("href") or "") if link is not None else None
        if match is None:
            continue
        proposicao_hash = match.group(1)
        if proposicao_hash in seen:
            continue
        seen.add(proposicao_hash)
        labelled = _labelled_rows(card)
        cards.append(
            PropositionCard(
                proposicao_hash=proposicao_hash,
                codigo=text_of(link) or None,
                ementa=text_of(find(card, "p", cls="fst-italic")) or None,
                data_entrada=parse_date(labelled.get("Entrada")),
                situacao=clean(labelled.get("Situação atual")),
                autoria=tuple(
                    text_of(li) for li in find_all(card, "li") if text_of(li)
                ),
            )
        )
    return cards


def _labelled_rows(card: Node) -> dict[str, str]:
    """``<div class="row"><div class="fw-bold">Entrada</div><div>06/08/2026</div></div>``."""
    out: dict[str, str] = {}
    for row in find_all(card, "div", cls="row"):
        cols = [c for c in row.children if isinstance(c, Node) and c.tag == "div"]
        if len(cols) < 2:
            continue
        label = text_of(cols[0])
        if label and "fw-bold" in cols[0].classes:
            out.setdefault(label, text_of(cols[1]))
    return out


# ── Electoral blackout ───────────────────────────────────────────────────────
# 🚨 Individual profile pages ({alesc_site_base}/deputado/{slug}/) are DOWN for the
# electoral blackout: they 302 to /aviso-periodo-eleitoral/ ("esta página permanecerá
# temporariamente indisponível"). Nothing in this package may depend on them.
# e-Legis and transparência are unaffected.
_BLACKOUT_MARKERS = (
    "aviso-periodo-eleitoral",
    "período eleitoral",
    "permanecerá temporariamente indisponível",
)


def is_electoral_blackout(body: str, url: str = "") -> bool:
    haystack = f"{url}\n{body[:20000]}".lower()
    return any(marker in haystack for marker in _BLACKOUT_MARKERS)
