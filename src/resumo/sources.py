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
Ato do Executivo     Same e-Legis route: a bill of executive initiative and a
                     mensagem de veto are filed at the Assembly like any other
                     proposição and read back from the same URL.
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

`source_portals` applies the same rule one level up: the landing page of each of the
five sources, for the /sobre page.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    # Both of these are e-Legis rows: an act of executive initiative is filed at the
    # Assembly like any other, and is read back from the same URL. They differ in who
    # authored it, not in where it lives.
    if house in (House.ASSEMBLEIA, House.EXECUTIVO):
        # Stored ids are prefixed to stay out of Câmara's numeric id space; e-Legis
        # wants the bare hashid back.
        bare = proposition_id.removeprefix(ALESC_ID_PREFIX)
        if not bare:
            return None
        return f"{get_settings().alesc_elegis_base.rstrip('/')}/proposicoes/{bare}"
    return None


@dataclass(frozen=True)
class SourcePortal:
    """One official page a reader can open to reach a source for themselves."""

    label: str
    url: str


def source_portals() -> dict[str, tuple[SourcePortal, ...]]:
    """Landing pages of the five sources, keyed by the sigla the /sobre page prints.

    Same rule as `proposition_url`: a link that lands on a search form or a soft 404
    is worse than no link. So these are the *portals* — the page from which a reader
    reaches the dataset — never the machine endpoints the collectors call, which
    answer JSON or a ZIP to a browser. Each was opened and shown to render the portal
    it claims, except the TSE ones: `tse.jus.br` answers 403 to every request from a
    datacenter IP, so that host is verifiable only from a residential browser. The
    ALESC and Câmara portals come from configuration, because a moved host there is
    an env var, not a deploy.
    """
    settings = get_settings()
    camara = settings.camara_portal_base.rstrip("/")
    return {
        # A CKAN portal: the same host the collector queries under /api/3/action.
        "tse": (SourcePortal("dados abertos do TSE", "https://dadosabertos.tse.jus.br/"),),
        "camara": (
            SourcePortal("dados abertos", "https://dadosabertos.camara.leg.br/"),
            # A presença em plenário é publicada por deputado, dentro da ficha de
            # cada um — o índice é a página de quem são os deputados.
            SourcePortal("deputados e presença", f"{camara}/deputados/quem-sao"),
            SourcePortal("gastos parlamentares", f"{camara}/transparencia/gastos-parlamentares"),
        ),
        "senado": (
            SourcePortal("dados abertos", "https://www12.senado.leg.br/dados-abertos"),
            SourcePortal("transparência", "https://www12.senado.leg.br/transparencia"),
        ),
        "alesc": (
            SourcePortal("e-Legis", f"{settings.alesc_elegis_base.rstrip('/')}/"),
            SourcePortal("transparência", f"{settings.alesc_transparencia_base.rstrip('/')}/"),
        ),
        "cgu": (
            SourcePortal(
                "emendas parlamentares",
                "https://portaldatransparencia.gov.br/download-de-dados/emendas-parlamentares",
            ),
        ),
    }
