"""Leitura do *relatório de presença em plenário* da Câmara — a única fonte oficial
brasileira que publica **dias faltados** já contados.

🚨 **Isto não é a API.** ``dadosabertos.camara.leg.br`` não expõe frequência: o único
recurso próximo é ``/eventos/{id}/deputados``, que lista **apenas quem compareceu**
(ausência não é representável ali) e mistura plenário com comissão sem campo de tipo.
Quem publica a conta oficial é o portal, em HTML:

    {camara_portal_base}/deputados/{id}/presenca-plenario/{ano}

A página traz duas tabelas. A **primeira** é o diário (uma linha por data, com as
sessões daquele dia) e tem ``rowspan`` — um parser ingênuo conta a mesma célula
várias vezes e chega a 291 "Presença" onde houve 96 dias. A **segunda** é o resumo
oficial da Mesa, com os seis números já reconciliados (inclusive justificativas
aceitas depois da sessão). Só a segunda é lida aqui, de propósito.

Estados que a página assume, todos verificados ao vivo (2026-08-19):

* deputado e ano com dados        -> HTTP 200, tabela-resumo presente;
* ano fora do exercício do mandato -> HTTP 200 com "Não há dados disponíveis para o
  ano de {ano}" e **sem** tabela-resumo. Isso **não** é zero falta: é ausência de
  dado, e vira ``None``, nunca uma linha de zeros;
* id de deputado inexistente      -> HTTP 500 (a página não usa 404);
* URL sem o segmento ``/{ano}``   -> HTTP 404.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from resumo.ingestion.html import find_all, parse_html, text_of
from resumo.util import parse_int
from resumo.util import normalize_name as _fold

# Os seis rótulos da tabela-resumo, verificados ao vivo. O casamento é por trecho
# normalizado (sem acento, sem caixa, sem o asterisco da nota de rodapé) porque a
# tabela não tem classe nenhuma que a distinga — é o único ponto deste projeto em
# que selecionar por texto é inevitável, e por isso :func:`parse_presenca_plenario`
# levanta erro quando NENHUM rótulo casa: uma reescrita do portal tem que aparecer
# como falha de coletor, não como um resumo vazio que parece "sem faltas".
#
# A ordem importa: "nao justificadas" contém "justificadas", então é testado antes.
_LABELS: tuple[tuple[str, str], ...] = (
    ("SESSOES DELIBERATIVAS COM ORDEM DO DIA INICIADA NA SESSAO LEGISLATIVA", "sessoes_total"),
    ("AUSENCIAS NAO JUSTIFICADAS EM SESSOES DELIBERATIVAS", "sessoes_ausencia_nao_justificada"),
    ("DIAS COM SESSOES DELIBERATIVAS REALIZADAS", "dias_total"),
    ("DIAS COM PRESENCA", "dias_presenca"),
    ("DIAS COM AUSENCIAS NAO JUSTIFICADAS", "dias_ausencia_nao_justificada"),
    ("DIAS COM AUSENCIAS JUSTIFICADAS", "dias_ausencia_justificada"),
)

_SEM_DADOS = re.compile(r"N[ãa]o h[áa] dados dispon[íi]veis", re.IGNORECASE)


class CamaraParseError(RuntimeError):
    """O relatório não tinha a forma que este módulo sabe ler.

    Levantado (nunca um AttributeError solto) para que o coletor transforme uma
    mudança do portal em CollectorResult de erro em vez de stack trace — ou, pior,
    em zeros silenciosos numa ficha pública.
    """


@dataclass(frozen=True)
class PresencaPlenario:
    """Os seis números do resumo oficial, para um deputado num ano.

    As duas réguas convivem e **não batem de propósito**: um deputado ausente na
    extraordinária nº 277 e presente na nº 278 conta como ausência de *sessão* e como
    *dia* com presença. Por isso as duas são guardadas, cada uma com sua unidade, e
    nenhuma é convertida na outra.
    """

    ano: int
    sessoes_total: int | None = None
    sessoes_ausencia_nao_justificada: int | None = None
    dias_total: int | None = None
    dias_presenca: int | None = None
    dias_ausencia_justificada: int | None = None
    dias_ausencia_nao_justificada: int | None = None

    @property
    def dias_presenca_efetiva(self) -> int | None:
        """Presença em dias, caindo para o complemento quando a linha não veio.

        O portal sempre publicou as quatro linhas de dia; o complemento existe para
        não perder o ano inteiro se uma delas sumir."""
        if self.dias_presenca is not None:
            return self.dias_presenca
        if self.dias_total is None:
            return None
        ausentes = (self.dias_ausencia_justificada or 0) + (self.dias_ausencia_nao_justificada or 0)
        return max(self.dias_total - ausentes, 0)

    @property
    def sessoes_presenca(self) -> int | None:
        """Presença em sessões: o que a Mesa publica é o total e as ausências NÃO
        justificadas, então a presença é o complemento — e é só isso que se pode
        afirmar. Uma ausência justificada em sessão não é publicada nesta régua, e
        inventá-la a partir da régua de dias misturaria as duas."""
        if self.sessoes_total is None:
            return None
        return max(self.sessoes_total - (self.sessoes_ausencia_nao_justificada or 0), 0)


def parse_presenca_plenario(markup: str, *, ano: int) -> PresencaPlenario | None:
    """Extrai o resumo oficial da página, ou ``None`` quando o ano não tem dados.

    ``None`` e um resumo zerado são coisas diferentes e a diferença é pública: o
    primeiro significa "o parlamentar não estava em exercício neste ano" e o segundo,
    "esteve, e não faltou".
    """
    root = parse_html(markup)
    tables = find_all(root, "table", cls="table-bordered")
    if not tables:
        # Página servida sem tabela alguma: só é aceitável se ela disser que não há
        # dados. Qualquer outra coisa é o portal tendo mudado de forma.
        if _SEM_DADOS.search(markup or ""):
            return None
        raise CamaraParseError("relatório de presença sem <table class='table-bordered'>")

    # O resumo é a ÚLTIMA tabela; a(s) anterior(es) são o diário com rowspan.
    values: dict[str, int] = {}
    for row in find_all(tables[-1], "tr"):
        cells = find_all(row, "td")
        if len(cells) < 2:
            continue
        label = _fold(text_of(cells[0]).replace("*", " ")) or ""
        value = parse_int(text_of(cells[1]))
        if value is None:
            continue
        for needle, field in _LABELS:
            if field not in values and needle in label:
                values[field] = value
                break

    if not values:
        if _SEM_DADOS.search(markup or ""):
            return None
        raise CamaraParseError(
            "tabela-resumo encontrada, mas nenhum dos rótulos conhecidos casou — "
            "o portal mudou a redação e o coletor precisa ser revisto"
        )
    return PresencaPlenario(ano=ano, **values)
