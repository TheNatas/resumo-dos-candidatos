"""Collector: Câmara /deputados/{id}/despesas -> Expense (CEAP / Cota Parlamentar)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Expense, House
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.camara.common import mandate_map
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import content_hash, record_ingestion, upsert
from resumo.util import clean, parse_decimal, parse_int


def _expense_row(member_id: str, mandate_id, d: dict) -> dict:
    row = {
        "mandate_id": mandate_id,
        "house": House.CAMARA,
        "house_member_id": member_id,
        "ano": parse_int(d.get("ano")) or 0,
        "mes": parse_int(d.get("mes")),
        "parcela": parse_int(d.get("parcela")),
        "tipo_despesa": clean(d.get("tipoDespesa")),
        "valor_documento": parse_decimal(d.get("valorDocumento")),
        "valor_liquido": parse_decimal(d.get("valorLiquido")),
        "valor_glosa": parse_decimal(d.get("valorGlosa")),
        "cnpj_cpf_fornecedor": clean(d.get("cnpjCpfFornecedor")),
        "nome_fornecedor": clean(d.get("nomeFornecedor")),
        "cod_documento": str(d.get("codDocumento") or ""),
        "num_documento": str(d.get("numDocumento") or ""),
        "url_documento": clean(d.get("urlDocumento")),
    }
    # Deterministic identity over the fields that make a CEAP line item distinct.
    row["row_hash"] = content_hash(
        "|".join(
            str(row[k])
            for k in (
                "house_member_id", "ano", "mes", "parcela", "cod_documento",
                "num_documento", "valor_documento", "valor_liquido", "valor_glosa",
                "cnpj_cpf_fornecedor", "tipo_despesa",
            )
        )
    )
    return row


class DespesasCollector(Collector):
    name = "camara_despesas"

    def run(
        self,
        session: Session,
        *,
        anos: list[int] | None = None,
        id_legislatura: int | None = None,
        client: CamaraClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        leg = id_legislatura or get_settings().id_legislatura
        anos = anos or [2023, 2024, 2025]
        owns = client is None
        client = client or CamaraClient()
        try:
            mandates = mandate_map(session, leg)
            members = list(mandates.items())
            if limit:
                members = members[:limit]

            total = 0
            for member_id, mandate_id in members:
                for ano in anos:
                    throttle()
                    rows = [
                        _expense_row(member_id, mandate_id, d)
                        for d in client.paginate(
                            f"deputados/{member_id}/despesas", {"ano": ano, "ordem": "ASC"}
                        )
                    ]
                    total += upsert(session, Expense, rows, index_elements=["row_hash"])
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{get_settings().camara_api_base}/deputados/*/despesas?anos={anos}",
                digest=f"count={total}",
                row_count=total,
            )
            return CollectorResult(self.name, "ingested", total, f"{len(members)} deputies")
        finally:
            if owns:
                client.close()
