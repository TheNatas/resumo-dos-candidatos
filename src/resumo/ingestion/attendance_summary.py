"""Collector: consolida :class:`AttendanceRecord` -> :class:`AttendanceSummary`.

Onde a Casa não publica um relatório de frequência, o resumo é **derivado** do que já
foi coletado sessão a sessão — e não há requisição de rede nenhuma aqui: este coletor
lê o próprio banco. Ele existe como coletor, e não como uma query, porque o resultado
é uma tabela materializada com proveniência (`derivation`, `source_url`, ledger),
exatamente como as outras, e porque precisa rodar depois delas no mesmo cron.

Duas Casas passam por aqui:

* **Senado** (``senado_votacao_comparecimento``) — os códigos de comparecimento das
  votações nominais. Uma linha de `attendance_record` já **é** uma sessão
  (``id_evento = SF{codigoSessao}``), então contar linhas é contar sessões.
* **ALESC** (``alesc_sessao_presenca``) — a folha de presença de cada sessão plenária.

A Câmara **não** passa por aqui, de propósito: `attendance_record` só recebe presenças
dela (``/eventos/{id}/deputados`` não devolve ausentes), e agregar isso produziria
100% para todo deputado federal. A frequência da Câmara vem do relatório oficial, em
:mod:`resumo.ingestion.camara.presenca_plenario`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo import attendance as att
from resumo.config import get_settings
from resumo.db.models import AttendanceSummary, House, Mandate
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import content_hash, record_ingestion, upsert


def _senado_source_url() -> str:
    return f"{get_settings().senado_api_base.rstrip('/')}/votacao"


def _alesc_source_url() -> str:
    return f"{get_settings().alesc_elegis_base.rstrip('/')}/sessoes-plenarias/*/presenca"


# Por derivação: nome do coletor (o que vai para o ledger) e a URL upstream que
# originou as linhas. `source_url` aponta para a fonte real, nunca para o banco:
# quem audita a ficha precisa chegar ao documento da Casa, não à nossa tabela.
SOURCES: dict[str, tuple[str, House, object]] = {
    "senado_votacao_comparecimento": ("senado_presenca_resumo", House.SENADO, _senado_source_url),
    "alesc_sessao_presenca": ("alesc_presenca_resumo", House.ASSEMBLEIA, _alesc_source_url),
}


class AttendanceSummaryCollector(Collector):
    """Consolida uma derivação de presença. Idempotente e sem rede."""

    def __init__(self, derivation: str):
        if derivation not in SOURCES:
            raise ValueError(
                f"derivação {derivation!r} não é consolidável — conhecidas: "
                f"{', '.join(sorted(SOURCES))}"
            )
        self.derivation = derivation
        self.name, self.house, self._source_url = SOURCES[derivation]
        self.metrica = att.DERIVATION_METRIC[derivation][0]

    def run(self, session: Session, **_) -> CollectorResult:
        by_mandate = att.summarize_records(session, derivation=self.derivation)
        if not by_mandate:
            undated = att.count_undated(session, derivation=self.derivation)
            detail = f"nenhum registro de presença com derivação {self.derivation!r}"
            if undated:
                detail += f" com data ({undated} sem data)"
            return CollectorResult(self.name, "empty", 0, detail)

        members = dict(
            session.execute(
                select(Mandate.id, Mandate.house_member_id).where(
                    Mandate.id.in_(list(by_mandate))
                )
            ).all()
        )
        source_url = self._source_url()
        rows = [
            row.as_row(
                mandate_id=mandate_id,
                house=self.house,
                house_member_id=members[mandate_id],
                metrica=self.metrica,
                derivation=self.derivation,
                source_url=source_url,
            )
            for mandate_id, summary_rows in by_mandate.items()
            if mandate_id in members
            for row in summary_rows
        ]
        total = upsert(
            session,
            AttendanceSummary,
            rows,
            index_elements=["mandate_id", "ano", "ambito", "unidade"],
        )

        undated = att.count_undated(session, derivation=self.derivation)
        record_ingestion(
            session,
            collector_name=self.name,
            source_url=source_url,
            # Dígito sobre os próprios números consolidados: uma recoleta que não
            # mudou nada produz o mesmo hash, e o ledger mostra o no-op.
            digest=content_hash(
                "\n".join(
                    sorted(
                        f"{r['mandate_id']}|{r['ano']}|{r['unidade'].value}|{r['total']}|"
                        f"{r['presenca']}|{r['ausencia_justificada']}|"
                        f"{r['ausencia_nao_classificada']}"
                        for r in rows
                    )
                )
            ),
            row_count=total,
        )
        anos = sorted({r["ano"] for r in rows})
        detail = f"{len(by_mandate)} mandatos · anos {anos[0]}–{anos[-1]}" if anos else "0 anos"
        if undated:
            # Uma fonte que passe a omitir a data da sessão esvazia o resumo sem
            # quebrar nada — por isso o número aparece no resultado do coletor.
            detail += f" · {undated} registro(s) sem data, fora do resumo"
        return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
