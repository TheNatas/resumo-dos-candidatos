"""Collector: acts of the governor before the Assembly -> Proposition.

Source of truth: ``{alesc_elegis_base}/proposicoes/processo-legislativo?
iniciativa=governador-do-estado&inicio=YYYY-MM-DD&fim=YYYY-MM-DD`` — the same
paginated e-Legis card listing the deputy collector reads, under the *institutional*
initiative value the portal publishes for the executive. Verified live 2026-08-28:
**510 acts** in the 2023-2026 term.

Two kinds of act come back, and both are the governor's own signature:

* ``PL.`` / ``PLC`` / ``PEC`` — bills of **executive initiative**, sent to the
  Assembly by the governor.
* ``MSV`` — **mensagens de veto**: "Veto Total ao Projeto de Lei nº 0287/2026…",
  "Veto Parcial…". A veto is the most purely executive act the Assembly publishes,
  and it is the one a reader is least able to find on their own.

🚨 **``iniciativa=governador-do-estado`` names the OFFICE, not the person.** e-Legis
has no concept of who held it. Attribution is therefore by *date*: an act is credited
to the mandate whose term window contains its Entrada, and an act falling outside
every known term is skipped and counted, never parked on the nearest governor. For a
term served start to finish by one person this is exact; it is also what keeps a
mid-term succession (resignation to run for another office, impeachment) from silently
crediting a predecessor's vetoes to their successor.

🚨 **The ``ano`` parameter is silently ignored on this query.** The deputy collector
partitions its crawl with ``?ano=`` and that works there; here
``?iniciativa=governador-do-estado&ano=2024`` returns the same 10.552 rows as
``&ano=2026`` — every act of every governorship e-Legis holds. The term window must be
expressed with ``inicio``/``fim``, in **ISO** form: ``inicio=01/01/2023`` is not merely
ignored, it renders an empty page with no total at all. Both were checked live.

🚨 **This source is ALESC, so it is Santa Catarina only.** A governor of another state
gets a mandate row from `executivo.governadores` (national, from the TSE) and no acts
from here. That asymmetry is deliberate — the incumbency claim generalizes, the track
record does not — and the collector says so rather than appearing to have found
nothing.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo import cargos
from resumo.config import get_settings
from resumo.db.models import House, Mandate, Proposition
from resumo.ingestion.alesc.client import AlescClient
from resumo.ingestion.alesc.common import proposition_id
from resumo.ingestion.alesc.parsing import (
    next_page_url,
    parse_proposition_cards,
    parse_result_total,
    split_codigo,
)
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.util import clean, normalize_name

logger = logging.getLogger("resumo.ingestion.executivo")

TRACK = "processo-legislativo"
INICIATIVA = "governador-do-estado"

# The only state whose assembly this collector can read. ALESC is the source; there is
# no equivalent crawl for the other 26 UFs.
SIGLA_UF = "SC"

# Every card must name this under *Autoria*. A far stronger guard than the statistical
# sentinel the deputy collector needs: because the initiative value is institutional,
# the expected author is a known constant, so each row can be checked individually
# instead of the listing being checked as a whole.
AUTORIA_ESPERADA = normalize_name("Governador do Estado")


def _term_window(mandate: Mandate) -> tuple[dt.date, dt.date]:
    """``(posse, fim)`` of an executive mandate, from the term it was seeded with.

    `Mandate.id_legislatura` carries the term's start year for `House.EXECUTIVO`, so
    the window is recoverable from the row without re-deriving it from settings — the
    crawl and the attribution then cannot disagree about which term they are in.
    """
    posse = mandate.data_inicio or dt.date(mandate.id_legislatura, 1, 1)
    return posse, dt.date(mandate.id_legislatura + cargos.TERM_YEARS - 1, 12, 31)


def _by_date(mandates: list[Mandate]) -> list[tuple[dt.date, dt.date, Mandate]]:
    return [(*_term_window(m), m) for m in mandates]


def _owner(windows, when: dt.date | None) -> Mandate | None:
    """The mandate holding office on `when`, or None (including for an undated card)."""
    if when is None:
        return None
    return next((m for start, end, m in windows if start <= when <= end), None)


class AtosCollector(Collector):
    name = "executivo_atos"

    def run(
        self,
        session: Session,
        *,
        year: int | None = None,
        client: AlescClient | None = None,
        max_pages: int = 120,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        election = year or cargos.previous_general_election(settings.election_year)
        posse, fim = cargos.executive_term(election)

        mandates = list(
            session.execute(
                select(Mandate).where(
                    Mandate.house == House.EXECUTIVO,
                    Mandate.sigla_uf == SIGLA_UF,
                    Mandate.id_legislatura == posse.year,
                )
            ).scalars()
        )
        if not mandates:
            return CollectorResult(
                self.name, "empty", 0,
                f"nenhum mandato EXECUTIVO de {SIGLA_UF} para {posse.year}- em base — "
                "rode `collect executivo-governadores` antes",
            )
        windows = _by_date(mandates)

        owns = client is None
        client = client or AlescClient()
        try:
            total, skipped_autoria, skipped_janela = self._collect(
                session, client, windows, posse=posse, fim=fim, max_pages=max_pages
            )
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=(
                    f"{settings.alesc_elegis_base}/proposicoes/{TRACK}"
                    f"?iniciativa={INICIATIVA}&inicio={posse}&fim={fim}"
                ),
                digest=f"count={total}",
                row_count=total,
            )
            detail = f"{SIGLA_UF} · mandato {posse}..{fim}"
            if skipped_autoria:
                detail += f" · {skipped_autoria} card(s) com outra autoria descartado(s)"
            if skipped_janela:
                detail += f" · {skipped_janela} fora da janela do mandato"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()

    def _sentinel(self, client: AlescClient, *, posse: dt.date, fim: dt.date) -> int | None:
        """Advertised total for the SAME window with no ``iniciativa``.

        e-Legis answers an unrecognised initiative with the whole Assembly rather than
        an error, so "did the filter apply?" has to be asked explicitly. Compared per
        crawl, not per card.
        """
        markup = client.get_elegis(
            f"/proposicoes/{TRACK}", {"inicio": posse.isoformat(), "fim": fim.isoformat(), "page": 1}
        )
        return parse_result_total(markup)

    def _collect(
        self, session, client, windows, *, posse, fim, max_pages
    ) -> tuple[int, int, int]:
        unfiltered = self._sentinel(client, posse=posse, fim=fim)

        path: str | None = f"/proposicoes/{TRACK}"
        params: dict | None = {
            "iniciativa": INICIATIVA,
            "inicio": posse.isoformat(),
            "fim": fim.isoformat(),
            "page": 1,
        }
        total = skipped_autoria = skipped_janela = 0
        first_page = True

        for _ in range(max_pages):
            if path is None:
                break
            markup = client.get_elegis(path, params)
            params = None  # subsequent pages arrive as fully-formed rel=next hrefs
            cards = parse_proposition_cards(markup)
            if not cards:
                break

            if first_page:
                announced = parse_result_total(markup)
                if unfiltered is not None and announced == unfiltered:
                    logger.error(
                        "%s: `iniciativa=%s` devolveu a listagem SEM filtro (%s linhas) "
                        "— nada atribuído (seriam proposições de toda a Casa)",
                        self.name, INICIATIVA, announced,
                    )
                    return 0, 0, 0
                logger.info(
                    "%s: %s atos de %s em %s..%s", self.name, announced, INICIATIVA, posse, fim
                )
                first_page = False

            rows = []
            for card in cards:
                # Per-card authorship check. The listing filter having applied does not
                # promise every row on it is the governor's, and a bill this platform
                # credits to a person must say so on its own face.
                if not any(normalize_name(a) == AUTORIA_ESPERADA for a in card.autoria):
                    skipped_autoria += 1
                    logger.warning(
                        "%s: %s tem autoria %s, não %r — descartado",
                        self.name, card.codigo, card.autoria, "Governador do Estado",
                    )
                    continue
                mandate = _owner(windows, card.data_entrada)
                if mandate is None:
                    skipped_janela += 1
                    logger.warning(
                        "%s: %s (Entrada %s) está fora da janela de todo mandato "
                        "conhecido — descartado em vez de atribuído ao mais próximo",
                        self.name, card.codigo, card.data_entrada,
                    )
                    continue
                sigla, numero, card_ano = split_codigo(card.codigo)
                rows.append(
                    {
                        "proposition_id": proposition_id(card.proposicao_hash),
                        # The AUTHORING body, matching `authoring_mandate_id`. The act
                        # is filed at the Assembly, but it is not the Assembly's.
                        "house": House.EXECUTIVO,
                        "authoring_mandate_id": mandate.id,
                        "sigla_tipo": (sigla or "")[:16] or None,
                        "numero": numero,
                        "ano": card_ano,
                        "ementa": clean(card.ementa),
                        "data_apresentacao": card.data_entrada,
                        "situacao": (clean(card.situacao) or "")[:255] or None,
                    }
                )
            total += upsert(session, Proposition, rows, index_elements=["proposition_id"])
            path = next_page_url(markup)

        return total, skipped_autoria, skipped_janela
