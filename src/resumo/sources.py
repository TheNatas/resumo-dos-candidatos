"""Back-links to the official page a row came from.

A number on the ficha is only auditable if the reader can reach the document behind
it, so every detail row that *can* carry a link does. The rule for adding one here is
that the URL was opened and shown to render the right record — **a link that lands on
a search form or a soft 404 is worse than no link**, because it spends the reader's
trust and returns nothing. What each source actually does, checked against the live
sites:

===================  ==========================================================
Câmara proposição    ``/proposicoesWeb/fichadetramitacao?idProposicao=`` — ok.
ALESC proposição     ``{e-Legis}/proposicoes/{hash}`` — ok (302 to
                     ``/tramitacoes``, which is the page a reader wants anyway).
Senado proposição    **No link.** ``/web/atividade/materias/-/materia/{id}``
                     answers 200 "Pesquisas - Senado Federal" for a real id and
                     for ``SF0000000`` alike: a soft 404 that cannot be told
                     apart from a hit. `Proposition.proposition_id` holds the
                     *processo* id, which that legacy route does not accept.
Votação (qualquer)   **No link.** ``camara.leg.br/votacoes/{id}`` 404s, and
                     ALESC's ``/extrato-votacao/{hash}`` answers 405 — it only
                     exists as an htmx fragment. A vote is linked through the
                     *proposição* it decided instead, which is the document the
                     reader is after.
Despesa              Whatever ``Expense.url_documento`` holds — the scanned
                     receipt, published by the Casa. Never constructed here.
===================  ==========================================================
"""

from __future__ import annotations

from resumo.config import get_settings
from resumo.db.models import House
from resumo.ingestion.alesc.common import ID_PREFIX as ALESC_ID_PREFIX


def proposition_url(house: House | None, proposition_id: str | None) -> str | None:
    """Public page for one proposição, or None when the source has no usable one."""
    if not proposition_id or house is None:
        return None
    if house is House.CAMARA:
        return (
            "https://www.camara.leg.br/proposicoesWeb/fichadetramitacao"
            f"?idProposicao={proposition_id}"
        )
    if house is House.ASSEMBLEIA:
        # Stored ids are prefixed to stay out of Câmara's numeric id space; e-Legis
        # wants the bare hashid back.
        bare = proposition_id.removeprefix(ALESC_ID_PREFIX)
        if not bare:
            return None
        return f"{get_settings().alesc_elegis_base.rstrip('/')}/proposicoes/{bare}"
    return None
