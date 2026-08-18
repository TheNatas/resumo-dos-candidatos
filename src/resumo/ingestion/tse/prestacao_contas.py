"""Collector: TSE prestação de contas eleitorais -> campaign finance tables.

Source of truth: the TSE bulk zip
``.../odsele/prestacao_contas/prestacao_de_contas_eleitorais_candidatos_<ANO>.zip``
— ONE national artifact per election year; there are no per-UF zips (``..._SC.zip``
is a 404). The UF split lives *inside* the zip, exactly like ``consulta_cand``, so
``parsing.iter_records(..., ufs=...)`` already narrows it for us.

The zip carries four CSV families (each × 27 UFs + ``_BRASIL``) plus leiame PDFs:

===========================================  ====================================
``receitas_candidatos_*``                     receipts/donations  (has SQ_CANDIDATO)
``despesas_contratadas_candidatos_*``         committed expenses  (has SQ_CANDIDATO)
``despesas_pagas_candidatos_*``               payments            (NO candidate col)
``receitas_candidatos_doador_originario_*``   pass-through donors (NO candidate col)
===========================================  ====================================

The two candidate-less families resolve through ``SQ_PRESTADOR_CONTAS`` (the
accounting entity), using a prestador -> candidato map built from the other two.

Two traps worth naming, because both silently corrupt totals:

* ``SQ_DESPESA`` is NOT unique — one contract yields many line items (installments,
  multi-line invoices), repeating up to ~90x in BOTH despesas families, and the
  relation between contratadas and pagas is many-to-many. Hence the row-hash
  identity on those two models. Any downstream ``SUM`` must aggregate **per side**
  to ``sq_despesa`` before joining, or the totals fan out.
* The FINAL file RETAINS earlier parcial/relatório rows (2022/SC: Final 58124,
  Parcial 181, Relatório Financeiro 12, Regularização da Omissão 3). Aggregations
  must filter on ``tp_prestacao_contas`` or the same money is counted twice.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from sqlalchemy import String, select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import (
    AccountFiling,
    CampaignExpense,
    CampaignPayment,
    CampaignRevenue,
    CampaignRevenueOriginator,
    Candidacy,
)
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import download_to_tempfile
from resumo.ingestion.ledger import (
    already_ingested,
    content_hash,
    record_ingestion,
    scoped_key,
    upsert,
)
from resumo.ingestion.tse import parsing
from resumo.util import clean, normalize_name, parse_date, parse_decimal, parse_int

logger = logging.getLogger("resumo.ingestion.tse")

# The CDN *directory* is `prestacao_contas` but the *file* is `prestacao_de_contas_
# eleitorais_candidatos_<ANO>.zip` — using `prestacao_de_contas` as the directory
# 404s for every year. That mismatch is also why `ckan.cdn_url()` cannot be reused:
# it builds `{base}/{produto}/{produto}_{ano}.zip`, i.e. it assumes directory ==
# file stem, which holds for consulta_cand/bem_candidato but not here.
CDN_DIR = "prestacao_contas"
FILE_STEM = "prestacao_de_contas_eleitorais_candidatos"


def cdn_url(year: int) -> str:
    """The national prestação de contas zip for `year`."""
    base = get_settings().tse_cdn_base.rstrip("/")
    return f"{base}/{CDN_DIR}/{FILE_STEM}_{year}.zip"


# ── CSV family dispatch ──────────────────────────────────────────────────────
# Order matters: `receitas_candidatos_doador_originario_*` also matches the plain
# `receitas_candidatos` prefix, so it has to be tested first.
RECEITAS = "receitas"
ORIGINARIOS = "doador_originario"
CONTRATADAS = "despesas_contratadas"
PAGAS = "despesas_pagas"

_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (ORIGINARIOS, re.compile(r"receitas_candidatos_doador_originario", re.I)),
    (RECEITAS, re.compile(r"receitas_candidatos", re.I)),
    (CONTRATADAS, re.compile(r"despesas_contratadas", re.I)),
    (PAGAS, re.compile(r"despesas_pagas", re.I)),
)


def family_of(member: str) -> str | None:
    """Which CSV family a zip member belongs to, or None if unrecognized."""
    name = Path(member).name
    for family, pattern in _FAMILIES:
        if pattern.search(name):
            return family
    return None


# ── Value normalization ──────────────────────────────────────────────────────
# `util.clean` already drops #NULO/#NULO#/-1/-3; this product also uses -4 as a
# "not informed" sentinel (and -4 in a money column would otherwise parse to a
# real negative value).
_EXTRA_NULLISH = {"-4", "-4,00", "-4.00"}


def _clean(value: str | None) -> str | None:
    v = clean(value)
    return None if v is not None and v in _EXTRA_NULLISH else v


def _int(value: str | None) -> int | None:
    return parse_int(_clean(value))


def _date(value: str | None):
    return parse_date(_clean(value))


def _decimal(value: str | None) -> float | None:
    return parse_decimal(_clean(value))


# TP_PRESTACAO_CONTAS letter-casing differs BETWEEN FILES and BETWEEN YEARS
# ("ORDINÁRIA"/"FINAL" in receitas vs "Ordinária"/"Final" in despesas), so the key
# is casefolded and accent-stripped (`normalize_name`) before mapping.
_FILING_BY_KEY: dict[str, AccountFiling] = {
    "FINAL": AccountFiling.final,
    "PARCIAL": AccountFiling.parcial,
    "RELATORIO FINANCEIRO": AccountFiling.relatorio_financeiro,
    "REGULARIZACAO DA OMISSAO": AccountFiling.regularizacao_omissao,
    "REGULARIZACAO DE OMISSAO": AccountFiling.regularizacao_omissao,
    "REGULARIZACAO OMISSAO": AccountFiling.regularizacao_omissao,
}
_unknown_filings: set[str] = set()


def parse_filing(value: str | None) -> AccountFiling:
    """Map TP_PRESTACAO_CONTAS to :class:`AccountFiling`, case/accent-insensitively."""
    key = normalize_name(_clean(value))
    if not key:
        return AccountFiling.outro
    filing = _FILING_BY_KEY.get(key)
    if filing is not None:
        return filing
    # Tolerate wording drift ("RELATORIO FINANCEIRO DE CAMPANHA", ...) before
    # falling back to `outro`, which is a silent bucket — so log it once.
    for known, mapped in _FILING_BY_KEY.items():
        if key.startswith(known):
            return mapped
    if key not in _unknown_filings:
        _unknown_filings.add(key)
        logger.warning("Unmapped TP_PRESTACAO_CONTAS %r -> AccountFiling.outro", value)
    return AccountFiling.outro


class _RowHasher:
    """Deterministic identity for the families whose natural key repeats.

    `SQ_DESPESA` is not unique, so identity is a hash over the whole normalized row.
    Rows that are byte-identical to another line item still deserve their own row
    (two identical R$500 installments are two payments), so exact duplicates get an
    occurrence ordinal. The ordinal is order-independent — the rows it separates are
    identical — hence stable across re-runs, which keeps the upsert idempotent.
    """

    def __init__(self) -> None:
        self._seen: Counter[str] = Counter()

    def __call__(self, family: str, row: dict) -> str:
        payload = family + "|" + "|".join(f"{k}={row[k]!s}" for k in sorted(row))
        seq = self._seen[payload]
        self._seen[payload] += 1
        return content_hash(f"{payload}|#{seq}")


def _fit_columns(model, rows: Sequence[dict]) -> None:
    """Clip strings to the declared column widths.

    TSE free-text (razão social, descrição do CNAE) occasionally overflows the
    declared width; clipping one field beats failing the whole batch.
    """
    limits = {
        c.name: c.type.length
        for c in model.__table__.columns
        if isinstance(c.type, String) and c.type.length
    }
    for row in rows:
        for name, limit in limits.items():
            value = row.get(name)
            if isinstance(value, str) and len(value) > limit:
                row[name] = value[:limit]


# ── Row builders ─────────────────────────────────────────────────────────────
def _revenue_row(r: dict[str, str], year: int, row_hash: _RowHasher) -> dict | None:
    """Build one receipt row.

    Identity is a row hash, not SQ_RECEITA: the sequence repeats across genuinely
    different receipts (same candidate, turno and filing, different value and
    description), so keying on it drops real declared revenue.
    """
    sq_receita = _clean(r.get("SQ_RECEITA"))
    if not sq_receita:
        return None
    row = {
        "sq_receita": sq_receita,
        "sq_candidato": _clean(r.get("SQ_CANDIDATO")),
        "sq_prestador_contas": _clean(r.get("SQ_PRESTADOR_CONTAS")),
        "ano_eleicao": _int(r.get("AA_ELEICAO")) or year,
        # ST_TURNO, not NR_TURNO — and it arrives quoted ("1"/"2").
        "st_turno": _int(r.get("ST_TURNO")),
        "tp_prestacao_contas": parse_filing(r.get("TP_PRESTACAO_CONTAS")),
        "dt_prestacao_contas": _date(r.get("DT_PRESTACAO_CONTAS")),
        "dt_receita": _date(r.get("DT_RECEITA")),
        "vr_receita": _decimal(r.get("VR_RECEITA")),
        "ds_receita": _clean(r.get("DS_RECEITA")),
        "ds_fonte_receita": _clean(r.get("DS_FONTE_RECEITA")),
        "ds_origem_receita": _clean(r.get("DS_ORIGEM_RECEITA")),
        "ds_natureza_receita": _clean(r.get("DS_NATUREZA_RECEITA")),
        "ds_especie_receita": _clean(r.get("DS_ESPECIE_RECEITA")),
        "nr_cpf_cnpj_doador": _clean(r.get("NR_CPF_CNPJ_DOADOR")),
        "nm_doador": _clean(r.get("NM_DOADOR")),
        "nm_doador_rfb": _clean(r.get("NM_DOADOR_RFB")),
        "ds_cnae_doador": _clean(r.get("DS_CNAE_DOADOR")),
        "sg_uf_doador": _clean(r.get("SG_UF_DOADOR")),
        "nm_municipio_doador": _clean(r.get("NM_MUNICIPIO_DOADOR")),
        "sq_candidato_doador": _clean(r.get("SQ_CANDIDATO_DOADOR")),
        "sg_partido_doador": _clean(r.get("SG_PARTIDO_DOADOR")),
    }
    row["row_hash"] = row_hash(RECEITAS, row)
    return row


def _originator_row(r: dict[str, str]) -> dict | None:
    sq_receita = _clean(r.get("SQ_RECEITA"))
    if not sq_receita:
        return None
    return {
        "sq_receita": sq_receita,
        # Part of the unique key, so it must never be NULL.
        "nr_cpf_cnpj_doador_originario": _clean(r.get("NR_CPF_CNPJ_DOADOR_ORIGINARIO")) or "",
        "nm_doador_originario": _clean(r.get("NM_DOADOR_ORIGINARIO")),
        "nm_doador_originario_rfb": _clean(r.get("NM_DOADOR_ORIGINARIO_RFB")),
        "tp_doador_originario": _clean(r.get("TP_DOADOR_ORIGINARIO")),
        "ds_cnae_doador_originario": _clean(r.get("DS_CNAE_DOADOR_ORIGINARIO")),
        "vr_receita": _decimal(r.get("VR_RECEITA")),
    }


def _expense_row(r: dict[str, str], year: int, row_hash: _RowHasher) -> dict | None:
    sq_despesa = _clean(r.get("SQ_DESPESA"))
    prestador = _clean(r.get("SQ_PRESTADOR_CONTAS"))
    if not sq_despesa and not prestador:
        return None
    row = {
        "sq_despesa": sq_despesa,
        "sq_candidato": _clean(r.get("SQ_CANDIDATO")),
        "sq_prestador_contas": prestador,
        "ano_eleicao": _int(r.get("AA_ELEICAO")) or year,
        "st_turno": _int(r.get("ST_TURNO")),
        "tp_prestacao_contas": parse_filing(r.get("TP_PRESTACAO_CONTAS")),
        "dt_despesa": _date(r.get("DT_DESPESA")),
        "vr_despesa_contratada": _decimal(r.get("VR_DESPESA_CONTRATADA")),
        "ds_despesa": _clean(r.get("DS_DESPESA")),
        "ds_origem_despesa": _clean(r.get("DS_ORIGEM_DESPESA")),
        "ds_tipo_documento": _clean(r.get("DS_TIPO_DOCUMENTO")),
        "nr_documento": _clean(r.get("NR_DOCUMENTO")),
        "nr_cpf_cnpj_fornecedor": _clean(r.get("NR_CPF_CNPJ_FORNECEDOR")),
        "nm_fornecedor": _clean(r.get("NM_FORNECEDOR")),
        "nm_fornecedor_rfb": _clean(r.get("NM_FORNECEDOR_RFB")),
        "ds_cnae_fornecedor": _clean(r.get("DS_CNAE_FORNECEDOR")),
        "sg_uf_fornecedor": _clean(r.get("SG_UF_FORNECEDOR")),
        "nm_municipio_fornecedor": _clean(r.get("NM_MUNICIPIO_FORNECEDOR")),
    }
    row["row_hash"] = row_hash(CONTRATADAS, row)
    return row


def _payment_row(r: dict[str, str], year: int, row_hash: _RowHasher) -> dict | None:
    sq_despesa = _clean(r.get("SQ_DESPESA"))
    prestador = _clean(r.get("SQ_PRESTADOR_CONTAS"))
    if not sq_despesa and not prestador:
        return None
    row = {
        "sq_despesa": sq_despesa,
        "sq_parcelamento_despesa": _clean(r.get("SQ_PARCELAMENTO_DESPESA")),
        "sq_prestador_contas": prestador,
        # This family has NO candidate column; backfilled from the prestador map.
        "sq_candidato": None,
        "ano_eleicao": _int(r.get("AA_ELEICAO")) or year,
        "st_turno": _int(r.get("ST_TURNO")),
        "tp_prestacao_contas": parse_filing(r.get("TP_PRESTACAO_CONTAS")),
        "dt_pagto_despesa": _date(r.get("DT_PAGTO_DESPESA")),
        # VR_PAGTO_DESPESA — not VR_PAGAMENTO.
        "vr_pagto_despesa": _decimal(r.get("VR_PAGTO_DESPESA")),
        "ds_despesa": _clean(r.get("DS_DESPESA")),
        "ds_natureza_despesa": _clean(r.get("DS_NATUREZA_DESPESA")),
        "ds_especie_recurso": _clean(r.get("DS_ESPECIE_RECURSO")),
        "ds_fonte_despesa": _clean(r.get("DS_FONTE_DESPESA")),
        "ds_origem_despesa": _clean(r.get("DS_ORIGEM_DESPESA")),
    }
    row["row_hash"] = row_hash(PAGAS, row)
    return row


def _prestador_map(rows: Iterable[dict]) -> dict[str, str]:
    """SQ_PRESTADOR_CONTAS -> SQ_CANDIDATO, from the two families that carry both.

    Verified 1:1 on 2022/SC (950 prestadores, none mapping to more than one
    candidacy). A prestador that ever maps to two candidacies is dropped from the
    map and logged rather than guessed at.
    """
    seen: dict[str, set[str]] = {}
    for row in rows:
        prestador, candidato = row.get("sq_prestador_contas"), row.get("sq_candidato")
        if prestador and candidato:
            seen.setdefault(prestador, set()).add(candidato)
    mapping = {}
    for prestador, candidatos in seen.items():
        if len(candidatos) == 1:
            mapping[prestador] = next(iter(candidatos))
        else:
            logger.warning(
                "SQ_PRESTADOR_CONTAS %s maps to %d candidacies (%s) — left unresolved",
                prestador,
                len(candidatos),
                ",".join(sorted(candidatos)),
            )
    return mapping


def _existing_receipts(session: Session, wanted: set[str]) -> set[str]:
    """Which of `wanted` already exist in campaign_revenue (chunked IN)."""
    found: set[str] = set()
    ids = list(wanted)
    for start in range(0, len(ids), 5000):
        chunk = ids[start : start + 5000]
        stmt = select(CampaignRevenue.sq_receita).where(CampaignRevenue.sq_receita.in_(chunk))
        found.update(sq for (sq,) in session.execute(stmt))
    return found


class PrestacaoContasCollector(Collector):
    name = "tse_prestacao_contas"

    def run(
        self,
        session: Session,
        *,
        source: Path | str | None = None,
        year: int | None = None,
        ufs: list[str] | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        year = year or settings.election_year
        uf_scope = tuple(u.upper() for u in ufs) if ufs is not None else settings.uf_list

        # Resolve the artifact (local file for tests/backfill, else download).
        # NOTE: no CKAN lookup here. There is no `prestacao-de-contas-eleitorais-2026`
        # package (package_show -> Not Found), so `ckan.resolve_resource_url` would
        # only ever burn a request before falling back — we hit the CDN directly.
        tmp: Path | None = None
        if source is not None:
            data_path: Path | str = source
            digest = content_hash(Path(source).read_bytes())
            source_url = str(source)
        else:
            source_url = cdn_url(year)
            tmp, digest = download_to_tempfile(source_url)
            data_path = tmp

        ledger_url = scoped_key(source_url, uf=",".join(uf_scope))

        try:
            if already_ingested(session, ledger_url, digest):
                return CollectorResult(self.name, "skipped", 0, "unchanged (hash match)")

            revenues: dict[str, dict] = {}
            originators: list[dict] = []
            expenses: list[dict] = []
            payments: list[dict] = []
            unknown_members: set[str] = set()
            row_hash = _RowHasher()

            # `parsing._select_members` prefers the per-UF members whenever a UF
            # filter is set, and only falls back to `_BRASIL` when no per-UF member
            # matches — which is exactly what this product needs: `_BRASIL` is the
            # national UNION (every UF plus SG_UF='BR' presidential rows), so reading
            # it *and* the per-UF files would double-count every row.
            for r in parsing.iter_records(data_path, ufs=uf_scope):
                family = family_of(r.get("__source_file", ""))
                if family == RECEITAS:
                    if rev := _revenue_row(r, year, row_hash):
                        revenues[rev["row_hash"]] = rev
                elif family == ORIGINARIOS:
                    if orig := _originator_row(r):
                        originators.append(orig)
                elif family == CONTRATADAS:
                    if exp := _expense_row(r, year, row_hash):
                        expenses.append(exp)
                elif family == PAGAS:
                    if pay := _payment_row(r, year, row_hash):
                        payments.append(pay)
                else:
                    unknown_members.add(r.get("__source_file", "?"))

            for member in sorted(unknown_members):
                logger.warning("Unrecognized member in prestação de contas zip: %s", member)

            # despesas_pagas carries no candidate column: resolve it through the
            # accounting entity, using the map the other two families provide.
            prestadores = _prestador_map([*revenues.values(), *expenses])
            unresolved: Counter[str] = Counter()
            for pay in payments:
                prestador = pay["sq_prestador_contas"]
                candidato = prestadores.get(prestador) if prestador else None
                if candidato is None:
                    # Keep the row (the money is real) but say so — never drop it
                    # silently just because the prestador did not resolve.
                    unresolved[prestador or "(sem prestador)"] += 1
                pay["sq_candidato"] = candidato
            if unresolved:
                logger.warning(
                    "%d payment rows across %d prestadores could not be attributed "
                    "to a candidacy (sample: %s)",
                    sum(unresolved.values()),
                    len(unresolved),
                    ", ".join(p for p, _ in unresolved.most_common(5)),
                )

            # FK integrity: `sq_candidato` FKs candidacy.sq_candidato, and the
            # candidacy table is UF/cargo-scoped, so most of the national zip points
            # at candidacies we never ingested. Null the reference out instead of
            # dropping the row — the prestador/fornecedor data stays inspectable.
            known = {sq for (sq,) in session.execute(select(Candidacy.sq_candidato))}
            orphaned = 0
            for row in (*revenues.values(), *expenses, *payments):
                if row["sq_candidato"] is not None and row["sq_candidato"] not in known:
                    row["sq_candidato"] = None
                    orphaned += 1

            revenue_rows = list(revenues.values())
            _fit_columns(CampaignRevenue, revenue_rows)
            _fit_columns(CampaignExpense, expenses)
            _fit_columns(CampaignPayment, payments)

            n_rev = upsert(session, CampaignRevenue, revenue_rows, index_elements=["row_hash"])
            n_exp = upsert(session, CampaignExpense, expenses, index_elements=["row_hash"])
            n_pay = upsert(session, CampaignPayment, payments, index_elements=["row_hash"])

            # Originators reference a receipt by SQ_RECEITA. That is a join key, not
            # a foreign key (the sequence is not unique on either side), so keep only
            # the ones whose receipt we actually hold — this run's, or a prior scope's.
            n_orig = 0
            if originators:
                known_sq = {rev["sq_receita"] for rev in revenues.values()}
                wanted = {o["sq_receita"] for o in originators} - known_sq
                keep = known_sq | _existing_receipts(session, wanted)
                kept = [o for o in originators if o["sq_receita"] in keep]
                _fit_columns(CampaignRevenueOriginator, kept)
                n_orig = upsert(
                    session,
                    CampaignRevenueOriginator,
                    kept,
                    index_elements=["sq_receita", "nr_cpf_cnpj_doador_originario"],
                )

            total = n_rev + n_exp + n_pay + n_orig
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=ledger_url,
                digest=digest,
                row_count=total,
            )

            detail = (
                f"{n_rev} receitas · {n_exp} despesas contratadas · {n_pay} pagas · "
                f"{n_orig} doadores originários · uf={','.join(uf_scope) or 'ALL'}"
            )
            if orphaned:
                detail += f" · {orphaned} rows outside the ingested candidacy scope"
            if unresolved:
                detail += f" · {sum(unresolved.values())} payments without a candidacy"
            if total == 0:
                # Expected before the filing windows open (2026: parcial 9-13 Sep,
                # contas finais 3 Nov) — the zip is published with header-only CSVs.
                return CollectorResult(
                    self.name, "empty", 0, f"no rows in artifact · uf={','.join(uf_scope) or 'ALL'}"
                )
            return CollectorResult(self.name, "ingested", total, detail)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
