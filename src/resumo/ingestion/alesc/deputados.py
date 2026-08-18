"""Collector: ALESC roster -> Mandate (+ seeds Person identity).

Source of truth: ``{alesc_site_base}/wp-admin/admin-ajax.php?action=alm_get_posts&
id=list_search&post_type=post_team&posts_per_page=50&page=N`` — 200 JSON
``{"html": "<article …>", "meta": {"postcount": 50, "totalposts": 61}}``. Each card
carries the profile **slug**, the display name (``h3.lab-title-news``) and the party
(``span.lab-button``). 61 > 40 because suplentes who assumed office are listed too.

🚨 This is an **undocumented WordPress plugin route** (Ajax Load More) and is the most
fragile thing in the ALESC set. Any shape drift is turned into a clear
``CollectorResult(status="error", …)`` naming what changed — never a stack trace.

🚨 **Individual profile pages are DOWN for the electoral blackout**:
``{alesc_site_base}/deputado/{slug}/`` 302s to ``/aviso-periodo-eleitoral/``. This
collector must not (and does not) read them. Pass ``fallback=True`` to fall back to
the e-Legis ``iniciativa`` <select> (~63 current-legislature slugs, no party) when the
admin-ajax route breaks; e-Legis is unaffected by the blackout.

🚨 **ALESC publishes no CPF and no birth date.** `Person.cpf` therefore stays ``None``
for every state deputy, `Person.data_nascimento` stays ``None``, and resolution to a
TSE candidacy is **name-based** — it can never reach the `cpf_exact` tier the way
Câmara-seeded people do. Do not invent an identifier here.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import House, Mandate, Person
from resumo.ingestion.alesc.client import AlescBlackoutError, AlescClient
from resumo.ingestion.alesc.parsing import (
    AlescParseError,
    RosterEntry,
    is_current_member_label,
    parse_iniciativa_options,
    parse_roster_payload,
)
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import record_ingestion
from resumo.util import clean, normalize_name

logger = logging.getLogger("resumo.ingestion.alesc")

ADMIN_AJAX = "/wp-admin/admin-ajax.php"
# House.ASSEMBLEIA is the generic state-assembly slot; the concrete state lives here.
SIGLA_UF = "SC"
_MAX_PAGES = 20  # 61 members / 50 per page — a runaway loop guard, not a real limit
# `mandate.house_member_id` is String(32). Real deputy slugs top out at 20 chars, but
# the e-Legis fallback list also contains committees and bancadas (up to 83 chars), so
# an over-long slug is rejected loudly instead of blowing up on INSERT.
_MAX_SLUG = 32


def _ajax_params(page: int, posts_per_page: int) -> dict[str, str | int]:
    return {
        "action": "alm_get_posts",
        "id": "list_search",
        "post_type": "post_team",
        "posts_per_page": posts_per_page,
        "page": page,
    }


def _fetch_roster(
    client: AlescClient, *, posts_per_page: int, max_pages: int
) -> tuple[list[RosterEntry], int | None]:
    """Paginate admin-ajax until `totalposts` is covered (or a page comes back empty)."""
    entries: list[RosterEntry] = []
    seen: set[str] = set()
    total: int | None = None
    for page in range(max_pages):
        payload = client.get_site_json(ADMIN_AJAX, _ajax_params(page, posts_per_page))
        page_entries, page_total = parse_roster_payload(payload)
        total = page_total if page_total is not None else total
        fresh = [e for e in page_entries if e.slug not in seen]
        if not fresh:
            break
        seen.update(e.slug for e in fresh)
        entries.extend(fresh)
        if total is not None and len(entries) >= total:
            break
    return entries, total


def _fallback_roster(client: AlescClient) -> list[RosterEntry]:
    """e-Legis ``iniciativa`` <select>: slugs only, no party, historical names filtered
    out by the ``Deputado``/``Deputada`` label prefix the current legislature carries."""
    markup = client.get_elegis("/proposicoes/atividade-parlamentar")
    entries = []
    for slug, label in parse_iniciativa_options(markup):
        if not is_current_member_label(label):
            continue
        nome = label.split(" ", 1)[1] if " " in label else label
        entries.append(RosterEntry(slug=slug, nome=nome.strip(), sigla_partido=None))
    return entries


def _get_or_create_person(session: Session, mandate: Mandate | None, nome: str) -> Person:
    """Seed identity from the display name alone — ALESC publishes nothing else.

    Reuse is restricted to CPF-less people so an ALESC deputy is never merged into a
    Câmara-seeded person (who always has a CPF) on a name collision. Real cross-source
    merging is the resolution pipeline's job, not this collector's.
    """
    if mandate is not None and mandate.person_id:
        person = session.get(Person, mandate.person_id)
        if person is not None:
            return person
    nome_norm = normalize_name(nome)
    person = None
    if nome_norm:
        person = session.execute(
            select(Person).where(Person.cpf.is_(None), Person.nome_normalizado == nome_norm)
        ).scalars().first()
    if person is None:
        person = Person(cpf=None, nome_normalizado=nome_norm, nome_civil=None)
        session.add(person)
        session.flush()
    return person


def _upsert_mandate(session: Session, entry: RosterEntry, leg: int) -> None:
    existing = session.execute(
        select(Mandate).where(
            Mandate.house == House.ASSEMBLEIA,
            Mandate.house_member_id == entry.slug,
            Mandate.id_legislatura == leg,
        )
    ).scalar_one_or_none()
    person = _get_or_create_person(session, existing, entry.nome)
    mandate = existing or Mandate(
        # house_member_id is the ALESC profile slug (e.g. "ana-campagnolo"): the
        # institution exposes NO numeric member id anywhere, on any host.
        house=House.ASSEMBLEIA,
        house_member_id=entry.slug,
        id_legislatura=leg,
    )
    mandate.person_id = person.id
    mandate.nome_parlamentar = clean(entry.nome) or mandate.nome_parlamentar
    mandate.sigla_partido = clean(entry.sigla_partido) or mandate.sigla_partido
    mandate.sigla_uf = SIGLA_UF
    # ALESC publishes neither titular/suplente status nor mandate dates on the roster;
    # the list simply includes suplentes who assumed office. Left unset rather than guessed.
    if existing is None:
        session.add(mandate)
    session.flush()


class DeputadosCollector(Collector):
    name = "alesc_deputados"

    def run(
        self,
        session: Session,
        *,
        id_legislatura: int | None = None,
        client: AlescClient | None = None,
        limit: int | None = None,
        posts_per_page: int = 50,
        max_pages: int = _MAX_PAGES,
        fallback: bool = False,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.alesc_id_legislatura
        owns = client is None
        client = client or AlescClient()
        source = "admin-ajax"
        try:
            try:
                entries, total = _fetch_roster(
                    client, posts_per_page=posts_per_page, max_pages=max_pages
                )
            except (AlescParseError, AlescBlackoutError) as exc:
                if not fallback:
                    return CollectorResult(
                        self.name, "error", 0,
                        f"roster unavailable via {ADMIN_AJAX}: {exc} "
                        "— retry with fallback=True to use the e-Legis iniciativa list",
                    )
                logger.warning("alesc_deputados: %s — falling back to e-Legis", exc)
                entries, total, source = _fallback_roster(client), None, "e-Legis iniciativa"

            if not entries and fallback and source == "admin-ajax":
                logger.warning("alesc_deputados: admin-ajax returned no cards — falling back")
                entries, source = _fallback_roster(client), "e-Legis iniciativa"

            if not entries:
                return CollectorResult(
                    self.name, "empty", 0,
                    f"{source} returned no deputy cards — the roster markup changed",
                )
            oversized = [e.slug for e in entries if len(e.slug) > _MAX_SLUG]
            if oversized:
                logger.warning(
                    "alesc_deputados: %s slug(s) longer than %s chars are not deputies "
                    "(committee/bancada entries?) — skipped: %s",
                    len(oversized), _MAX_SLUG, oversized[:5],
                )
                entries = [e for e in entries if len(e.slug) <= _MAX_SLUG]
            if limit:
                entries = entries[:limit]

            for entry in entries:
                _upsert_mandate(session, entry, leg)

            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{settings.alesc_site_base}{ADMIN_AJAX}?action=alm_get_posts",
                digest=f"count={len(entries)}",
                row_count=len(entries),
            )
            detail = f"legislatura {leg} · {source}"
            if total is not None and total != len(entries):
                detail += f" · upstream totalposts={total}"
            return CollectorResult(self.name, "ingested", len(entries), detail)
        finally:
            if owns:
                client.close()
