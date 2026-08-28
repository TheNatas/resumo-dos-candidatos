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
from resumo.ingestion.alesc.common import MandateIndex, mandate_index, proposition_id
from resumo.ingestion.alesc.parsing import (
    AlescParseError,
    next_page_url,
    parse_iniciativa_options,
    parse_proposition_cards,
    parse_result_total,
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


def resolve_iniciativa(
    client: AlescClient, index: MandateIndex, track: str
) -> tuple[dict[str, str], list[str]]:
    """``{roster slug -> e-Legis ``iniciativa`` value}``, plus the slugs with no value.

    🚨 **The WordPress profile slug and the e-Legis ``iniciativa`` value are two
    different vocabularies, and they disagree for about one deputy in nine.**
    ``Mandate.house_member_id`` holds the profile slug (``profa-vanessa-da-rosa``);
    e-Legis wants its own (``vanessa-da-rosa``). That alone would be a harmless miss
    if the filter failed loudly — but it does not:

    🚨 **e-Legis silently IGNORES an ``iniciativa`` it does not recognise and answers
    with the whole house.** ``?iniciativa=slug-que-nao-existe`` returns byte-identical
    cards to sending no filter at all. Feeding it a profile slug therefore yields
    every proposição in the Casa, which the caller would stamp with one deputy's
    ``authoring_mandate_id`` — 538 PLs of 2026, authored by forty different people,
    landing on one ficha as if they were hers. Wrong attribution, stated with total
    confidence, about a named person.

    So the value is resolved *before* any crawl, against the vocabulary e-Legis itself
    publishes in the ``iniciativa`` <select>, in falling order of confidence:

    1. **The roster slug is itself a published option** — string equality, no inference.
    2. **Exactly one *unclaimed* option resolves to this mandate by name**, via the same
       :class:`~resumo.ingestion.alesc.common.MandateIndex` that bridges nome civil and
       nome parlamentar everywhere else. "Deputado Padre Pedro Baldissera" ->
       ``pedro-baldissera``.
    3. Otherwise the deputy is **not crawled at all** and is reported. A roster gap
       costs one ficha its proposições; a wrong guess costs another ficha its truth.
    """
    try:
        options = parse_iniciativa_options(client.get_elegis(f"/proposicoes/{track}"))
    except AlescParseError:
        # O <select> sumiu ou mudou de forma. Sem o vocabulário publicado não dá para
        # validar nada de antemão — mas o sentinela de `_collect` detecta exatamente o
        # desfecho ruim (a listagem sem filtro) de forma independente. Então segue com
        # o slug do perfil e deixa a segunda linha de defesa trabalhar, em vez de parar
        # a coleta inteira por causa de uma mudança de markup.
        logger.warning(
            "%s: <select name='iniciativa'> ausente em %r — sem validação prévia; "
            "a atribuição fica por conta do sentinela de listagem sem filtro",
            ProposicoesCollector.name, track,
        )
        return {ref.slug: ref.slug for ref in index.refs}, []
    published = {value: label for value, label in options}
    roster = {ref.slug: ref for ref in index.refs}

    # 1. Exact: the profile slug happens to be a valid iniciativa value.
    resolved = {slug: slug for slug in roster if slug in published}

    # 2. By name, over what is left on BOTH sides. Restricting the pool matters: an
    #    option already claimed in step 1 must not also be offered to a second mandate.
    claimed = set(resolved.values())
    by_mandate: dict[str, set[str]] = {}
    for value, label in published.items():
        if value in claimed:
            continue
        ref = index.match(label, record_unmatched=False)
        if ref is not None and ref.slug not in resolved:
            by_mandate.setdefault(ref.slug, set()).add(value)
    for slug, values in by_mandate.items():
        # Two options resolving to one mandate is ambiguity, not a tie to break: one of
        # them is a co-authored or historical entry, and picking either is a coin flip.
        if len(values) == 1:
            resolved[slug] = next(iter(values))

    unresolved = sorted(slug for slug in roster if slug not in resolved)
    return resolved, unresolved


# Abaixo disso, "o deputado tem tantas quanto a Casa inteira" é coincidência plausível
# — num ano de poucas proposições um único autor pode de fato responder por todas — e
# descartar seria perder dado real. Acima, é o filtro que não pegou.
_UNFILTERED_FLOOR = 20


def _looks_unfiltered(markup, cards, sentinel) -> bool:
    """Did this response come back as if no ``iniciativa`` had been sent?

    O total anunciado ("Exibindo 1 - 10 de 531") é o sinal forte: ele separa as duas
    consultas mesmo quando a primeira página coincide. A identidade da página 1 só
    entra quando a fonte não anuncia total algum.
    """
    total_sem_filtro, ids_sem_filtro = sentinel
    if not ids_sem_filtro:
        return False
    total = parse_result_total(markup)
    if total is not None and total_sem_filtro is not None:
        return total == total_sem_filtro and total >= _UNFILTERED_FLOOR
    # Sem total publicado: só acusa com a página cheia, senão um deputado com três
    # proposições que por acaso são as três mais recentes da Casa seria descartado.
    return frozenset(c.proposicao_hash for c in cards) == ids_sem_filtro and len(cards) >= 10


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
            crawled: set[str] = set()
            for track in tracks:
                iniciativa, unresolved = resolve_iniciativa(client, index, track)
                if unresolved:
                    logger.warning(
                        "%s: %s de %s mandatos sem valor `iniciativa` em %r — NÃO "
                        "coletados (o filtro seria ignorado e devolveria a Casa "
                        "inteira): %s",
                        self.name, len(unresolved), len(index.refs), track,
                        ", ".join(unresolved),
                    )
                # A resposta SEM filtro, uma por (track, ano) — é contra ela que se
                # reconhece um filtro ignorado em silêncio. Tem de usar o mesmo `ano`
                # da consulta do deputado, senão compara duas listagens diferentes e
                # nunca acusa nada. Custa uma requisição por ano.
                sentinels = {ano: self._unfiltered(client, track, ano=ano) for ano in anos}
                for member in members:
                    value = iniciativa.get(member.slug)
                    if value is None:
                        continue
                    crawled.add(member.slug)
                    for ano in anos:
                        total += self._collect(
                            session, client, member, iniciativa=value, track=track,
                            ano=ano, max_pages=max_pages, sentinel=sentinels[ano],
                        )
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{settings.alesc_elegis_base}/proposicoes/*?iniciativa=*",
                digest=f"count={total}",
                row_count=total,
            )
            # Contados sobre quem foi de fato percorrido: `skipped` é da Casa inteira
            # e subtraí-lo de uma lista já cortada por `limit` daria um número errado.
            detail = (
                f"{len(crawled)}/{len(members)} deputies · "
                f"tracks={','.join(tracks)} · anos={anos[0]}..{anos[-1]}"
            )
            missing = sorted({m.slug for m in members} - crawled)
            if missing:
                detail += f" · {len(missing)} sem `iniciativa`"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()

    def _unfiltered(self, client, track, *, ano) -> tuple[int | None, frozenset[str]]:
        """The answer to the SAME query with no ``iniciativa``: advertised total and
        page-1 hashes. The shape a response must not have."""
        params = {"page": 1} | ({"ano": ano} if ano else {})
        markup = client.get_elegis(f"/proposicoes/{track}", params)
        return (
            parse_result_total(markup),
            frozenset(c.proposicao_hash for c in parse_proposition_cards(markup)),
        )

    def _collect(
        self, session, client, member, *, iniciativa, track, ano, max_pages, sentinel
    ) -> int:
        path: str | None = f"/proposicoes/{track}"
        params: dict | None = {"iniciativa": iniciativa, "ano": ano, "page": 1}
        total = 0
        first_page = True
        for _ in range(max_pages):
            if path is None:
                break
            markup = client.get_elegis(path, params)
            params = None  # subsequent pages come as fully-formed rel=next hrefs
            cards = parse_proposition_cards(markup)
            if not cards:
                break
            # Rede de segurança para o que `resolve_iniciativa` não prevê: um valor
            # legítimo que o servidor resolva ignorar assim mesmo. Se a resposta do
            # deputado é a mesma da consulta sem filtro, o que voltou é a Casa inteira
            # — e a Casa inteira não é a autoria de ninguém.
            if first_page and _looks_unfiltered(markup, cards, sentinel):
                logger.error(
                    "%s: `iniciativa=%s` devolveu a listagem SEM filtro em %r/%s — "
                    "nada atribuído a %s (seriam proposições de toda a Casa)",
                    self.name, iniciativa, track, ano, member.slug,
                )
                return 0
            first_page = False
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
