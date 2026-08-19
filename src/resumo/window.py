"""Janela incremental de coleta.

Votação antiga não muda. Ainda assim o coletor de votações da Câmara refazia, a
cada execução, as ~6.900 votações do ano inteiro mais duas chamadas por votação —
cerca de 14 mil requisições e três horas para reaprender fatos imutáveis. O ledger
`RawIngestion` torna a **escrita** no banco um no-op, mas não evita a rede.

Então a janela passa a depender do que já está em base: banco vazio pede o ano
todo; banco populado pede só o rabo recente. A sobreposição existe porque a fonte
publica votação com atraso — reler algumas semanas é barato, perder uma votação
que apareceu depois não é.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resumo.db.models import House, Mandate, Vote

DEFAULT_OVERLAP_DAYS = 30


def incremental_start(
    session: Session,
    house: House,
    *,
    floor: str,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
) -> str:
    """Data (ISO) de onde recomeçar a coleta de votações desta Casa.

    Por Casa, e não global: a ALESC publica em ritmo diferente da Câmara, e um
    máximo único faria a Casa mais lenta ser pulada pelo avanço da mais rápida.
    Nunca antes de `floor` — a janela encolhe, jamais se estende para trás.
    """
    ultimo = session.execute(
        select(func.max(Vote.data_votacao))
        .join(Mandate, Mandate.id == Vote.mandate_id)
        .where(Mandate.house == house)
    ).scalar()
    if ultimo is None:
        return floor
    retomada = (ultimo - dt.timedelta(days=overlap_days)).isoformat()
    return max(retomada, floor)
