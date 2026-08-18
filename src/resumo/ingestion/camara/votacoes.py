"""Collector: Câmara votações -> Vote (individual nominal votes).

There is no per-deputy vote endpoint: we list votações in a date window, then for
each fetch /votos (and /orientacoes for party guidance). Symbolic votings have an
empty /votos and are skipped.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Vote
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.camara.common import mandate_map
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.util import clean, parse_date

# A API recusa a janela inteira com 400 "A diferença entre as datas não pode ser
# maior que 3 meses". O limite é do endpoint, não de quem chama: fatiar aqui evita
# que todo caller (README, cron, CI) tenha que lembrar dele. 80 dias fica
# confortavelmente dentro de qualquer leitura de "3 meses".
_MAX_WINDOW_DAYS = 80


def date_windows(data_inicio: str, data_fim: str, *, max_days: int = _MAX_WINDOW_DAYS):
    """Fatia [início, fim] em janelas inclusivas de no máximo `max_days` dias."""
    start = dt.date.fromisoformat(data_inicio)
    end = dt.date.fromisoformat(data_fim)
    if start > end:
        return
    while start <= end:
        stop = min(start + dt.timedelta(days=max_days - 1), end)
        yield start.isoformat(), stop.isoformat()
        start = stop + dt.timedelta(days=1)


class VotacoesCollector(Collector):
    name = "camara_votacoes"

    def run(
        self,
        session: Session,
        *,
        data_inicio: str,
        data_fim: str,
        id_legislatura: int | None = None,
        client: CamaraClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        leg = id_legislatura or get_settings().id_legislatura
        # In a state-scoped install the mandate map holds only that state's members;
        # national roll-calls still list all 513, so rows for members we do not track
        # are dropped rather than stored with a dangling mandate_id.
        scoped = bool(get_settings().uf_list)
        owns = client is None
        client = client or CamaraClient()
        try:
            mandates = mandate_map(session, leg)
            votacoes = []
            for win_inicio, win_fim in date_windows(data_inicio, data_fim):
                votacoes.extend(
                    client.paginate(
                        "votacoes",
                        {
                            "dataInicio": win_inicio,
                            "dataFim": win_fim,
                            "ordem": "DESC",
                            "ordenarPor": "dataHoraRegistro",
                        },
                    )
                )
            if limit:
                votacoes = votacoes[:limit]

            total = 0
            for v in votacoes:
                id_votacao = str(v["id"])
                throttle()
                # Party orientation (Sim/Não per party) for fidelity analysis later.
                orient: dict[str, str] = {}
                try:
                    for o in client.get(f"votacoes/{id_votacao}/orientacoes").get("dados", []):
                        if o.get("siglaPartidoBloco"):
                            orient[o["siglaPartidoBloco"]] = clean(o.get("orientacaoVoto"))
                except Exception:  # noqa: BLE001 — orientations are optional enrichment
                    pass

                throttle()
                votos = client.get(f"votacoes/{id_votacao}/votos").get("dados", [])
                rows = []
                for voto in votos:
                    dep = voto.get("deputado_") or {}
                    member_id = str(dep.get("id") or "")
                    if not member_id or (scoped and member_id not in mandates):
                        continue
                    partido = clean(dep.get("siglaPartido"))
                    rows.append(
                        {
                            "mandate_id": mandates.get(member_id),
                            "house_member_id": member_id,
                            "id_votacao": id_votacao,
                            "id_proposicao": str(v.get("idProposicaoObjeto") or "") or None,
                            "tipo_voto": clean(voto.get("tipoVoto")),
                            "data_votacao": parse_date(voto.get("dataRegistroVoto") or v.get("data")),
                            "orientacao_partido": orient.get(partido) if partido else None,
                        }
                    )
                total += upsert(
                    session, Vote, rows, index_elements=["id_votacao", "house_member_id"]
                )
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{get_settings().camara_api_base}/votacoes?{data_inicio}..{data_fim}",
                digest=f"count={total}",
                row_count=total,
            )
            return CollectorResult(self.name, "ingested", total, f"{len(votacoes)} votações")
        finally:
            if owns:
                client.close()
