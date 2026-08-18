"""Collector: ALESC transparência bulk CSV -> Expense (verba de gabinete + diárias).

Source of truth: ``{alesc_transparencia_base}/{dataset}/csv/{ano}`` (2011..current).
No scraping, no auth, no pagination — this is the highest-value / lowest-fragility
ALESC source and is meant to be run first.

Format traps, all verified live:

* ``text/csv; charset=UTF-8`` **with a BOM** — decoded ``utf-8-sig`` by the client, or
  the first header reads ``\\ufeffVerba``.
* **semicolon**-delimited, values double-quoted, Brazilian money (``1.234,56``).
* 🚨 **Negative values are refunds** (``DIÁRIAS;"Devolução Diária Deputado";…;-539,00``).
  The sign is kept: abs()-ing them would inflate every total by double-counting money
  that was given back.

Two datasets are per-deputy attributable and are ingested:

* ``gabinetes-parlamentares`` — ``Verba;Descrição;Conta;Favorecido;Trecho;Data de
  Referência;Valor``. 🚨 **``Conta`` is the DEPUTY** (this is the CEAP analogue);
  ``Favorecido`` is the vendor and ``Trecho`` a travel leg.
* ``diarias`` — ``Nome;Conta;Vínculo;Data;Quantidade;Valor;Relatório``. A *different*
  schema (the spec's shared-columns assumption is wrong): ``Nome`` is the person,
  ``Conta`` a numeric payroll account and ``Vínculo`` is ``Deputado``/``Servidor``.
  Only ``Vínculo == Deputado`` rows are attributable, ~11% of the file.

``despesas`` is deliberately NOT ingested: it is the institution's *empenho* ledger
(``EMPENHO_NUMERO;…;CREDOR;…;VALOR_EMPENHADO``) with no deputy column at all — the
few deputy-linked rows only name one inside a free-text ``DESCRICAO``. Attributing it
per-deputy would be a guess, so it is reported as unsupported instead.

Columns with no home in :class:`~resumo.db.models.Expense` (``Descrição``, ``Trecho``,
``Quantidade``) participate in ``row_hash`` so distinct line items stay distinct, but
are not persisted — the model is owned elsewhere and is not extended from here.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from collections.abc import Iterator

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Expense, House
from resumo.ingestion.alesc.client import AlescClient
from resumo.ingestion.alesc.common import MandateIndex, mandate_index
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import (
    already_ingested,
    content_hash,
    record_ingestion,
    scoped_key,
    upsert,
)
from resumo.util import clean, parse_date, parse_decimal

logger = logging.getLogger("resumo.ingestion.alesc")

GABINETES = "gabinetes-parlamentares"
DIARIAS = "diarias"
SUPPORTED_DATASETS = (GABINETES, DIARIAS)
# Institution-level empenho ledger — no per-deputy column. See the module docstring.
UNSUPPORTED_DATASETS = {"despesas": "empenho ledger; no deputy column"}


def _default_anos() -> list[int]:
    """The 20th legislature to date (e-Legis and the site only cover 2023+)."""
    return list(range(2023, dt.date.today().year + 1))


def _read_rows(text: str) -> Iterator[dict[str, str]]:
    yield from csv.DictReader(io.StringIO(text), delimiter=";")


def _hash(dataset: str, slug: str, ano: int, raw: dict[str, str]) -> str:
    """Deterministic identity over the whole source row, so re-fetching is a no-op and
    two genuinely different line items never collapse."""
    parts = [dataset, slug, str(ano)] + [f"{k}={raw.get(k, '')}" for k in sorted(raw)]
    return content_hash("|".join(parts))


def _gabinete_row(index: MandateIndex, ano: int, raw: dict[str, str]) -> dict | None:
    ref = index.match(raw.get("Conta"))
    if ref is None:
        return None
    data = parse_date(raw.get("Data de Referência"))
    return {
        "row_hash": _hash(GABINETES, ref.slug, ano, raw),
        "mandate_id": ref.mandate_id,
        "house": House.ASSEMBLEIA,
        "house_member_id": ref.slug,
        "ano": data.year if data else ano,
        "mes": data.month if data else None,
        "parcela": None,
        "tipo_despesa": clean(raw.get("Verba")),
        # The source publishes a single figure: no gross/glosa split exists to record.
        "valor_documento": None,
        "valor_liquido": parse_decimal(raw.get("Valor")),
        "valor_glosa": None,
        "cnpj_cpf_fornecedor": None,  # never published
        "nome_fornecedor": clean(raw.get("Favorecido")),
        "cod_documento": "",
        "num_documento": "",
        "url_documento": None,
    }


def _diaria_row(index: MandateIndex, ano: int, raw: dict[str, str]) -> dict | None:
    if (clean(raw.get("Vínculo")) or "").upper() != "DEPUTADO":
        return None
    ref = index.match(raw.get("Nome"))
    if ref is None:
        return None
    data = parse_date(raw.get("Data"))
    relatorio = clean(raw.get("Relatório"))
    return {
        "row_hash": _hash(DIARIAS, ref.slug, ano, raw),
        "mandate_id": ref.mandate_id,
        "house": House.ASSEMBLEIA,
        "house_member_id": ref.slug,
        "ano": data.year if data else ano,
        "mes": data.month if data else None,
        "parcela": None,
        "tipo_despesa": "DIÁRIAS",
        "valor_documento": None,
        "valor_liquido": parse_decimal(raw.get("Valor")),
        "valor_glosa": None,
        "cnpj_cpf_fornecedor": None,
        "nome_fornecedor": None,
        "cod_documento": (clean(raw.get("Conta")) or "")[:32],
        "num_documento": (relatorio or "").rsplit("/", 1)[-1][:64],
        "url_documento": relatorio,
    }


_BUILDERS = {GABINETES: _gabinete_row, DIARIAS: _diaria_row}


class DespesasCollector(Collector):
    name = "alesc_despesas"

    def run(
        self,
        session: Session,
        *,
        anos: list[int] | None = None,
        datasets: list[str] | None = None,
        id_legislatura: int | None = None,
        client: AlescClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.alesc_id_legislatura
        anos = anos or _default_anos()
        wanted = list(datasets or SUPPORTED_DATASETS)
        for dataset in wanted:
            if dataset in UNSUPPORTED_DATASETS:
                logger.warning(
                    "alesc_despesas: dataset %r is not per-deputy attributable (%s) — skipped",
                    dataset, UNSUPPORTED_DATASETS[dataset],
                )
        wanted = [d for d in wanted if d in _BUILDERS]

        index = mandate_index(session, leg)
        if not index:
            return CollectorResult(
                self.name, "empty", 0,
                f"no ASSEMBLEIA mandates for legislatura {leg} — run alesc-deputados first",
            )

        owns = client is None
        client = client or AlescClient()
        try:
            total = 0
            skipped = 0
            for dataset in wanted:
                for ano in anos:
                    path = f"{dataset}/csv/{ano}"
                    url = f"{settings.alesc_transparencia_base}/{path}"
                    text, raw = client.get_transparencia_csv(path)
                    digest = content_hash(raw)
                    ledger_url = scoped_key(url, limit=limit)
                    if already_ingested(session, ledger_url, digest):
                        skipped += 1
                        continue

                    build = _BUILDERS[dataset]
                    rows = []
                    for record in _read_rows(text):
                        built = build(index, ano, record)
                        if built is not None:
                            rows.append(built)
                        if limit and len(rows) >= limit:
                            break
                    n = upsert(session, Expense, rows, index_elements=["row_hash"])
                    record_ingestion(
                        session,
                        collector_name=self.name,
                        source_url=ledger_url,
                        digest=digest,
                        row_count=n,
                    )
                    total += n

            unmatched = index.report_unmatched(self.name, "transparência CSV")
            detail = f"{len(wanted)} dataset(s) x {len(anos)} year(s)"
            if skipped:
                detail += f" · {skipped} unchanged"
            if unmatched:
                detail += f" · {unmatched}"
            status = "ingested" if total else ("skipped" if skipped else "empty")
            return CollectorResult(self.name, status, total, detail)
        finally:
            if owns:
                client.close()
