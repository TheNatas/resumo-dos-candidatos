"""Collector: CEAPS (Cota para o Exercício da Atividade Parlamentar) -> Expense.

CEAPS is published in two shapes and only one of them joins reliably:

* the static per-year CSV identifies the senator by a free-text UPPERCASE name,
  which fails to match ~12% of rows (accents, nome parlamentar vs nome civil,
  homonyms);
* the JSON API used here carries `codSenador`, which **is** `CodigoParlamentar` —
  a clean, non-null integer FK on every row.

So the join is exact and no fuzzy name matching enters the money data.

Note the host: CEAPS is served by the Senado's *administrative* portal, not by
legis.senado.leg.br, and it is the only Senado source that lives off-base — hence
the module constant below rather than a settings field.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Expense, House
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import content_hash, record_ingestion, upsert
from resumo.ingestion.senado.client import SenadoClient
from resumo.ingestion.senado.common import mandate_map
from resumo.util import clean, parse_decimal, parse_int

# CEAPS is NOT on senado_api_base (legis.senado.leg.br): it is published by the
# Senado's administrative open-data host. Kept here, next to its only consumer.
CEAPS_API_BASE = "https://adm.senado.gov.br/adm-dadosabertos"


def _expense_row(member_id: str, mandate_id, d: dict) -> dict:
    row = {
        "mandate_id": mandate_id,
        "house": House.SENADO,
        "house_member_id": member_id,
        "ano": parse_int(d.get("ano")) or 0,
        "mes": parse_int(d.get("mes")),
        # CEAPS has no "parcela" concept (that is a Câmara CEAP field).
        "parcela": None,
        "tipo_despesa": clean(d.get("tipoDespesa")),
        # Only the reimbursed amount is published: no gross value and no glosa, so
        # those stay NULL rather than being faked from valorReembolsado.
        "valor_documento": None,
        "valor_liquido": parse_decimal(d.get("valorReembolsado")),
        "valor_glosa": None,
        # ⚠️ The SUPPLIER's document, never the senator's — the Senado publishes no
        # CPF for parliamentarians anywhere.
        "cnpj_cpf_fornecedor": clean(d.get("cpfCnpj")),
        "nome_fornecedor": clean(d.get("fornecedor")),
        "cod_documento": str(d.get("id") or ""),
        "num_documento": str(d.get("documento") or ""),
        "url_documento": None,
    }
    # Deterministic identity over the fields that make a CEAPS line item distinct —
    # same contract as the Câmara collector so `row_hash` means one thing platform-wide.
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
    name = "senado_despesas"

    def run(
        self,
        session: Session,
        *,
        anos: list[int] | None = None,
        id_legislatura: int | None = None,
        client: SenadoClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.id_legislatura
        anos = anos or [2023, 2024, 2025]
        owns = client is None
        client = client or SenadoClient()
        try:
            mandates = mandate_map(session, leg)
            if limit:
                mandates = dict(list(mandates.items())[:limit])

            total = 0
            for ano in anos:
                throttle()
                # One request per year returns EVERY senator's lines (no pagination,
                # no per-senator filter), so the roster filter happens here.
                payload = client.get(f"{CEAPS_API_BASE}/api/v1/senadores/despesas_ceaps/{ano}")
                rows = []
                for d in payload if isinstance(payload, list) else []:
                    member_id = clean(d.get("codSenador"))
                    if not member_id or member_id not in mandates:
                        continue
                    rows.append(_expense_row(member_id, mandates[member_id], d))
                total += upsert(session, Expense, rows, index_elements=["row_hash"])

            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{CEAPS_API_BASE}/api/v1/senadores/despesas_ceaps/{{ano}}?anos={anos}",
                digest=f"count={total}",
                row_count=total,
            )
            return CollectorResult(self.name, "ingested", total, f"{len(mandates)} senadores")
        finally:
            if owns:
                client.close()
