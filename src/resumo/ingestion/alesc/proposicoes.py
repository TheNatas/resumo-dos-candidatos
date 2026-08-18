"""Collector: ALESC proposições by author -> Proposition.

Source of truth: ``{alesc_elegis_base}/proposicoes/{track}?iniciativa={slug}&ano={ano}
&page={n}`` — paginated cards ("Exibindo 1 - 10 de 531"), detail at
``/proposicoes/{hash}``.

**Two tracks, not one** (the spec only named the second; both accept the same
``iniciativa``/``ano`` filters and render identical cards):

* ``processo-legislativo`` — the actual bills: PL., PLC, PEC, PJL. This is the one
  that matters for a track record and is collected first.
* ``atividade-parlamentar`` — requerimentos, moções, indicações. An order of magnitude
  more rows (531 vs 112 for one deputy in one year) and far less substantive, so it is
  available but not part of the default track list.

🚨 Proposition ids are prefixed ``AL`` (:func:`~resumo.ingestion.alesc.common.
proposition_id`) because `proposition.proposition_id` is shared with Câmara, whose
ids are bare integers. e-Legis ids are opaque hashids (``KMmqv``) and would otherwise
be free to collide with a future numeric id.

The ``ano`` query parameter is a crawl-partitioning knob, **not** the proposition's
own year: ``?iniciativa=ze-caramori&ano=2026`` legitimately returns PLs from 2024 that
were still moving in 2026. `Proposition.ano` / `data_apresentacao` are therefore taken
from the card (código + *Entrada*), and sweeping several `anos` re-visits some rows —
harmless, because the upsert keys on the prefixed proposition id.

Co-authored propositions carry several names under *Autoria*, but
`Proposition.authoring_mandate_id` holds exactly one mandate: the row is attributed to
the deputy whose `iniciativa` query returned it, and re-running for a co-author simply
re-points it. The full author list stays visible in the source, not in this table.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import House, Proposition
from resumo.ingestion.alesc.client import AlescClient
from resumo.ingestion.alesc.common import mandate_index, proposition_id
from resumo.ingestion.alesc.parsing import (
    next_page_url,
    parse_proposition_cards,
    split_codigo,
)
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.util import clean

logger = logging.getLogger("resumo.ingestion.alesc")

PROCESSO_LEGISLATIVO = "processo-legislativo"
ATIVIDADE_PARLAMENTAR = "atividade-parlamentar"
DEFAULT_TRACKS = (PROCESSO_LEGISLATIVO,)
ALL_TRACKS = (PROCESSO_LEGISLATIVO, ATIVIDADE_PARLAMENTAR)


def _default_anos() -> list[int]:
    """The 20th legislature to date — e-Legis has nothing before Feb 2023."""
    return list(range(2023, dt.date.today().year + 1))


class ProposicoesCollector(Collector):
    name = "alesc_proposicoes"

    def run(
        self,
        session: Session,
        *,
        anos: list[int] | None = None,
        tracks: list[str] | None = None,
        id_legislatura: int | None = None,
        client: AlescClient | None = None,
        limit: int | None = None,
        max_pages: int = 60,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.alesc_id_legislatura
        anos = anos or _default_anos()
        tracks = [t for t in (tracks or list(DEFAULT_TRACKS)) if t in ALL_TRACKS]
        index = mandate_index(session, leg)
        if not index:
            return CollectorResult(
                self.name, "empty", 0,
                f"no ASSEMBLEIA mandates for legislatura {leg} — run alesc-deputados first",
            )

        members = index.refs[:limit] if limit else index.refs
        owns = client is None
        client = client or AlescClient()
        try:
            total = 0
            for member in members:
                for track in tracks:
                    for ano in anos:
                        total += self._collect(
                            session, client, member, track=track, ano=ano, max_pages=max_pages
                        )
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{settings.alesc_elegis_base}/proposicoes/*?iniciativa=*",
                digest=f"count={total}",
                row_count=total,
            )
            detail = f"{len(members)} deputies · tracks={','.join(tracks)} · anos={anos[0]}..{anos[-1]}"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()

    def _collect(self, session, client, member, *, track, ano, max_pages) -> int:
        path: str | None = f"/proposicoes/{track}"
        params: dict | None = {"iniciativa": member.slug, "ano": ano, "page": 1}
        total = 0
        for _ in range(max_pages):
            if path is None:
                break
            markup = client.get_elegis(path, params)
            params = None  # subsequent pages come as fully-formed rel=next hrefs
            cards = parse_proposition_cards(markup)
            if not cards:
                break
            rows = []
            for card in cards:
                sigla, numero, card_ano = split_codigo(card.codigo)
                rows.append(
                    {
                        "proposition_id": proposition_id(card.proposicao_hash),
                        "house": House.ASSEMBLEIA,
                        "authoring_mandate_id": member.mandate_id,
                        "sigla_tipo": (sigla or "")[:16] or None,
                        "numero": numero,
                        "ano": card_ano or ano,
                        "ementa": clean(card.ementa),
                        "data_apresentacao": card.data_entrada,
                        "situacao": (clean(card.situacao) or "")[:255] or None,
                    }
                )
            total += upsert(session, Proposition, rows, index_elements=["proposition_id"])
            path = next_page_url(markup)
        return total
