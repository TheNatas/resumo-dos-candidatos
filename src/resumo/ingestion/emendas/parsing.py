"""Parse the CGU bulk `EmendasParlamentares.zip` (latin-1, ';', all fields quoted).

Source of truth: https://dadosabertos-download.cgu.gov.br/PortalDaTransparencia/
saida/emendas-parlamentares/EmendasParlamentares.zip — refreshed monthly, ~32 MB.

The zip carries three CSVs; we read only the first:

* ``EmendasParlamentares.csv`` — the main table (~94k rows, 2014-2026), one row per
  **emenda x localidade x ação**. A single `Código da Emenda` legitimately repeats.
* ``EmendasParlamentares_Convenios.csv`` — convênio linkage (not ingested yet).
* ``EmendasParlamentares_PorFavorecido.csv`` — ~817k payment rows per *favorecido*.
  Not ingested yet, and when it is: the favorecido is the **vendor that got paid**,
  frequently not the beneficiary municipality, so it must never be presented as
  "where the money went" for a locality.

Column contract (verified against the real file on 2026-08-18 — the published header
differs from the CGU dictionary in three places, hence :func:`validate_header`):

* ``Número da emenda`` — lower-case "emenda", unlike every other column;
* ``Código Programa`` / ``Nome Programa`` — NOT "Programa Orçamentário";
* an extra ``Localidade de aplicação do recurso`` column sits between
  ``Número da emenda`` and ``Código Município IBGE``.

Sentinels: ``Sem informação`` (most columns), ``S/I`` (author/emenda numbers),
``-1`` (``Código UF IBGE``), ``Múltiplo`` (``UF``/``Município``). They are mapped to
``None`` — never stored as if they were values.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from unidecode import unidecode

from resumo.db.models import AmendmentType
from resumo.ingestion.ledger import content_hash
from resumo.util import clean, normalize_name, parse_decimal, parse_int

logger = logging.getLogger("resumo.ingestion.emendas")

ENCODING = "latin-1"
DELIMITER = ";"

# Members we must NOT read as the main table (matched case-insensitively).
_SIBLING_MARKERS = ("_convenios", "_porfavorecido")

# ── Column names, exactly as published ───────────────────────────────────────
COL_CODIGO_EMENDA = "Código da Emenda"
COL_ANO = "Ano da Emenda"
COL_TIPO = "Tipo de Emenda"
COL_AUTOR_CODIGO = "Código do Autor da Emenda"
COL_AUTOR_NOME = "Nome do Autor da Emenda"
COL_NUMERO = "Número da emenda"  # sic: lower-case "emenda" in the real header
COL_LOCALIDADE = "Localidade de aplicação do recurso"
COL_COD_MUNICIPIO = "Código Município IBGE"
COL_MUNICIPIO = "Município"
COL_COD_UF = "Código UF IBGE"
COL_UF = "UF"
COL_REGIAO = "Região"
COL_COD_FUNCAO = "Código Função"
COL_FUNCAO = "Nome Função"
COL_COD_SUBFUNCAO = "Código Subfunção"
COL_SUBFUNCAO = "Nome Subfunção"
COL_COD_PROGRAMA = "Código Programa"  # sic: not "Programa Orçamentário"
COL_PROGRAMA = "Nome Programa"
COL_COD_ACAO = "Código Ação"
COL_ACAO = "Nome Ação"
COL_COD_PLANO = "Código Plano Orçamentário"
COL_PLANO = "Nome Plano Orçamentário"
COL_EMPENHADO = "Valor Empenhado"
COL_LIQUIDADO = "Valor Liquidado"
COL_PAGO = "Valor Pago"
COL_RESTO_INSCRITO = "Valor Restos A Pagar Inscritos"
COL_RESTO_CANCELADO = "Valor Restos A Pagar Cancelados"
COL_RESTO_PAGO = "Valor Restos A Pagar Pagos"

# Every column the collector actually depends on. A missing one is a hard error:
# silently reading `None` out of a renamed column would be a data-integrity bug
# (it would zero out money or orphan the author).
REQUIRED_COLUMNS: tuple[str, ...] = (
    COL_CODIGO_EMENDA,
    COL_ANO,
    COL_TIPO,
    COL_AUTOR_CODIGO,
    COL_AUTOR_NOME,
    COL_COD_MUNICIPIO,
    COL_MUNICIPIO,
    COL_COD_UF,
    COL_UF,
    COL_REGIAO,
    COL_COD_FUNCAO,
    COL_FUNCAO,
    COL_COD_SUBFUNCAO,
    COL_SUBFUNCAO,
    COL_COD_PROGRAMA,
    COL_PROGRAMA,
    COL_COD_ACAO,
    COL_ACAO,
    COL_COD_PLANO,
    COL_PLANO,
    COL_EMPENHADO,
    COL_LIQUIDADO,
    COL_PAGO,
    COL_RESTO_INSCRITO,
    COL_RESTO_CANCELADO,
    COL_RESTO_PAGO,
)

# The grain of one row: emenda x localidade x ação x plano orçamentário. Verified
# unique over the whole national file (76,494 rows with a código -> 76,494 keys).
# The money columns are deliberately NOT part of it: the file is refreshed monthly
# and `Valor Empenhado` grows, so hashing it would insert a duplicate row every
# month instead of updating the existing one.
_HASH_COLUMNS: tuple[str, ...] = (
    COL_CODIGO_EMENDA,
    COL_ANO,
    COL_COD_MUNICIPIO,
    COL_COD_UF,
    COL_COD_FUNCAO,
    COL_COD_SUBFUNCAO,
    COL_COD_PROGRAMA,
    COL_COD_ACAO,
    COL_COD_PLANO,
)

# Source sentinels for "not informed", on top of the TSE ones `util.clean` knows.
_NULLISH = frozenset({"SEM INFORMACAO", "S/I", "N/I", "NAO INFORMADO", "-1", "-99"})

# `Município`/`Localidade` labels that are explicitly *not* a municipality.
_NOT_A_MUNICIPALITY = frozenset({"MULTIPLO", "MULTIPLOS", "NACIONAL", "EXTERIOR"})

_IBGE_MUNICIPIO = re.compile(r"^\d{7}$")

# 🚨 The `UF` column holds the FULL STATE NAME ("SANTA CATARINA"), never the "SC"
# sigla, so a naive `row["UF"] in ("SC",)` filter yields ZERO rows in silence.
# `get_settings().uf_list` speaks siglas, so the scope has to be translated here.
UF_SIGLA_TO_NOME: dict[str, str] = {
    "AC": "ACRE",
    "AL": "ALAGOAS",
    "AM": "AMAZONAS",
    "AP": "AMAPÁ",
    "BA": "BAHIA",
    "CE": "CEARÁ",
    "DF": "DISTRITO FEDERAL",
    "ES": "ESPÍRITO SANTO",
    "GO": "GOIÁS",
    "MA": "MARANHÃO",
    "MG": "MINAS GERAIS",
    "MS": "MATO GROSSO DO SUL",
    "MT": "MATO GROSSO",
    "PA": "PARÁ",
    "PB": "PARAÍBA",
    "PE": "PERNAMBUCO",
    "PI": "PIAUÍ",
    "PR": "PARANÁ",
    "RJ": "RIO DE JANEIRO",
    "RN": "RIO GRANDE DO NORTE",
    "RO": "RONDÔNIA",
    "RR": "RORAIMA",
    "RS": "RIO GRANDE DO SUL",
    "SC": "SANTA CATARINA",
    "SE": "SERGIPE",
    "SP": "SÃO PAULO",
    "TO": "TOCANTINS",
}

# `Código UF IBGE` is the 2-digit UF code zero-padded to 7 ("4200000" = SC). Used
# only as a *corroborating* signal (see `uf_verdict`), never as the filter itself.
UF_SIGLA_TO_CODIGO_IBGE: dict[str, str] = {
    "RO": "1100000", "AC": "1200000", "AM": "1300000", "RR": "1400000",
    "PA": "1500000", "AP": "1600000", "TO": "1700000", "MA": "2100000",
    "PI": "2200000", "CE": "2300000", "RN": "2400000", "PB": "2500000",
    "PE": "2600000", "AL": "2700000", "SE": "2800000", "BA": "2900000",
    "MG": "3100000", "ES": "3200000", "RJ": "3300000", "SP": "3500000",
    "PR": "4100000", "SC": "4200000", "RS": "4300000", "MS": "5000000",
    "MT": "5100000", "GO": "5200000", "DF": "5300000",
}

_NOME_NORM_TO_SIGLA: dict[str, str] = {
    normalize_name(nome): sigla for sigla, nome in UF_SIGLA_TO_NOME.items()
}
_CODIGO_TO_SIGLA: dict[str, str] = {
    codigo: sigla for sigla, codigo in UF_SIGLA_TO_CODIGO_IBGE.items()
}


class MissingColumnsError(ValueError):
    """The published header lost a column the collector depends on."""


class UnknownUfError(ValueError):
    """A configured UF sigla is not a Brazilian state."""


def validate_header(fieldnames: Iterable[str] | None, *, member: str = "") -> None:
    """Fail loudly (and legibly) when a depended-on column is gone."""
    found = [f.lstrip("﻿").strip() for f in (fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in found]
    if missing:
        where = f" in {member}" if member else ""
        raise MissingColumnsError(
            f"EmendasParlamentares header changed{where}: missing {missing!r}. "
            f"Header found: {found!r}"
        )


def cell(value: str | None) -> str | None:
    """`util.clean` plus the CGU sentinels ("Sem informação", "S/I", "-1", "-99").

    Compared with accents folded but punctuation intact — `util.normalize_name`
    would turn "S/I" into "S I" and "-1" into "1", and stop matching the sentinel.
    """
    v = clean(value)
    if v is None:
        return None
    return None if unidecode(v).strip().upper() in _NULLISH else v


def _cell_or_empty(row: dict[str, str], column: str) -> str:
    return cell(row.get(column)) or ""


# ── UF scope ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class UfScope:
    """Target states translated into what the source actually publishes."""

    siglas: tuple[str, ...]
    names: frozenset[str]  # normalized full state names ("SANTA CATARINA")
    codes: frozenset[str]  # `Código UF IBGE` values ("4200000")

    @property
    def is_national(self) -> bool:
        return not self.siglas


def uf_scope(siglas: Iterable[str] | None) -> UfScope:
    """Build the scope from siglas ("SC"). Empty/None means national (no filter)."""
    wanted = tuple(s.strip().upper() for s in (siglas or ()) if s and s.strip())
    unknown = [s for s in wanted if s not in UF_SIGLA_TO_NOME]
    if unknown:
        raise UnknownUfError(
            f"not Brazilian state siglas: {unknown!r} — expected one of "
            f"{sorted(UF_SIGLA_TO_NOME)}"
        )
    return UfScope(
        siglas=wanted,
        names=frozenset(normalize_name(UF_SIGLA_TO_NOME[s]) for s in wanted),
        codes=frozenset(UF_SIGLA_TO_CODIGO_IBGE[s] for s in wanted),
    )


def uf_verdict(row: dict[str, str], scope: UfScope) -> str:
    """"in_scope" | "code_only" | "out".

    The filter is name-based, because the name is what the source publishes. The
    "code_only" verdict exists so that a format change (CGU switching the column to
    siglas) surfaces as a loud warning instead of an empty ingestion: the row is
    still dropped, but the collector reports how many rows matched by IBGE code and
    not by name.
    """
    if scope.is_national:
        return "in_scope"
    if normalize_name(row.get(COL_UF)) in scope.names:
        return "in_scope"
    if cell(row.get(COL_COD_UF)) in scope.codes:
        return "code_only"
    return "out"


def sigla_for_uf(uf_name: str | None, codigo_uf_ibge: str | None = None) -> str | None:
    """Full state name (or IBGE code) -> sigla. None for "Múltiplo"/unknown."""
    sigla = _NOME_NORM_TO_SIGLA.get(normalize_name(uf_name) or "")
    if sigla:
        return sigla
    return _CODIGO_TO_SIGLA.get(cell(codigo_uf_ibge) or "")


# ── Tipo de emenda ───────────────────────────────────────────────────────────
@lru_cache(maxsize=256)
def map_tipo(raw: str | None) -> AmendmentType:
    """`Tipo de Emenda` -> :class:`AmendmentType`, by normalized prefix.

    Only the two *individual* modalities name a single legislator (RP6). Relator
    (RP8, struck down by the STF in Dec/2022 and absent after it), bancada (RP7, the
    whole state delegation) and comissão (RP9, a committee) are collective and must
    never be attributed to a person. Unknown strings degrade to `outro` and are
    logged once (the cache makes the warning fire once per distinct string).
    """
    norm = normalize_name(raw)
    if not norm:
        return AmendmentType.outro
    if norm.startswith("EMENDA INDIVIDUAL") or norm.startswith("INDIVIDUAL"):
        if "TRANSFERENCIA ESPECIAL" in norm or "TRANSFERENCIAS ESPECIAIS" in norm:
            return AmendmentType.individual_transferencia_especial
        if "FINALIDADE DEFINIDA" not in norm:
            logger.warning("emendas: unknown individual subtype %r -> finalidade definida", raw)
        return AmendmentType.individual_finalidade_definida
    if "BANCADA" in norm:
        return AmendmentType.bancada
    if "COMISSAO" in norm:
        return AmendmentType.comissao
    if "RELATOR" in norm:
        return AmendmentType.relator
    logger.warning("emendas: unknown Tipo de Emenda %r -> outro", raw)
    return AmendmentType.outro


# ── SIOP author code ─────────────────────────────────────────────────────────
_EMENDA_CODE = re.compile(r"^(\d{4})(\d{4})(\d{4})$")

# The code namespace is partitioned by first digit. Verified over the whole file:
# the tipo <-> prefix correspondence holds for every one of the 94,304 rows.
_CODE_KIND = {
    "1": "individual", "2": "individual", "3": "individual", "4": "individual",
    "9": "individual",
    "5": "comissao", "6": "comissao",  # 6xxx are Senado committees
    "7": "bancada",
    "8": "relator",
}


def author_code_from_emenda(codigo_emenda: str | None) -> str | None:
    """`202543010001` -> `4301` (ano + código do autor + sequencial)."""
    v = cell(codigo_emenda)
    m = _EMENDA_CODE.match(v) if v else None
    return m.group(2) if m else None


def author_code(row: dict[str, str]) -> str | None:
    """The SIOP author code: the explicit column, corroborated by the emenda code."""
    explicit = cell(row.get(COL_AUTOR_CODIGO))
    derived = author_code_from_emenda(row.get(COL_CODIGO_EMENDA))
    if explicit and derived and explicit.zfill(4) != derived:
        logger.warning(
            "emendas: author code mismatch — column %r vs emenda code %r (%s)",
            explicit, derived, cell(row.get(COL_CODIGO_EMENDA)),
        )
    return explicit or derived


def author_code_kind(code: str | None) -> str:
    """Structural sanity check on the code namespace: individual / comissao /
    bancada / relator / desconhecido."""
    c = cell(code)
    if not c or not c.isdigit():
        return "desconhecido"
    return _CODE_KIND.get(c[0], "desconhecido")


def is_structurally_individual(code: str | None) -> bool:
    return author_code_kind(code) == "individual"


# ── Row -> BudgetAmendment ───────────────────────────────────────────────────
def _municipio(row: dict[str, str]) -> tuple[str | None, str | None]:
    """(código IBGE, nome) — or (None, None) when the row is not municipal.

    Most rows are NOT tied to a municipality: for SC/2025 individual emendas only
    ~22 of 179 carry an IBGE code, the rest being "MÚLTIPLO" or "SANTA CATARINA
    (UF)". Those labels are dropped rather than stored, so nothing downstream can
    mistake "Múltiplo" for a city.
    """
    codigo = cell(row.get(COL_COD_MUNICIPIO))
    nome = cell(row.get(COL_MUNICIPIO))
    if not codigo or not _IBGE_MUNICIPIO.match(codigo):
        return None, None
    if nome and normalize_name(nome) in _NOT_A_MUNICIPALITY:
        return None, None
    return codigo, nome


def row_hash(row: dict[str, str]) -> str:
    return content_hash("|".join(_cell_or_empty(row, c) for c in _HASH_COLUMNS))


def amendment_row(row: dict[str, str]) -> dict | None:
    """Map one CSV row to a `BudgetAmendment` dict.

    Returns None when the row has no stable identity — `Código da Emenda` or
    `Ano da Emenda` missing. ~16k national rows (mostly 2014-2017) are published
    with "Sem informação" as the código *and* no author code; they cannot be keyed
    (any hash over the remaining fields would collide two distinct emendas and
    silently overwrite one's money) nor attributed, so they are dropped and counted.
    """
    codigo = cell(row.get(COL_CODIGO_EMENDA))
    ano = parse_int(row.get(COL_ANO))
    if not codigo or ano is None:
        return None

    codigo_municipio, municipio = _municipio(row)
    nome_autor = cell(row.get(COL_AUTOR_NOME))
    return {
        "row_hash": row_hash(row),
        "codigo_emenda": codigo,
        "ano": ano,
        "tipo_emenda_raw": cell(row.get(COL_TIPO)),
        "tipo": map_tipo(row.get(COL_TIPO)),
        "siop_author_code": author_code(row),
        "author_name_raw": nome_autor,
        "author_name_normalizado": normalize_name(nome_autor),
        "codigo_municipio_ibge": codigo_municipio,
        "municipio": municipio,
        "codigo_uf_ibge": cell(row.get(COL_COD_UF)),
        "uf": cell(row.get(COL_UF)),
        "regiao": cell(row.get(COL_REGIAO)),
        "nome_funcao": cell(row.get(COL_FUNCAO)),
        "nome_subfuncao": cell(row.get(COL_SUBFUNCAO)),
        "nome_programa": cell(row.get(COL_PROGRAMA)),
        "nome_acao": cell(row.get(COL_ACAO)),
        # NB: the source has NO "valor autorizado"/dotação column. `valor_empenhado`
        # is the best available proxy for "how much was committed".
        "valor_empenhado": parse_decimal(row.get(COL_EMPENHADO)),
        "valor_liquidado": parse_decimal(row.get(COL_LIQUIDADO)),
        "valor_pago": parse_decimal(row.get(COL_PAGO)),
        "valor_resto_inscrito": parse_decimal(row.get(COL_RESTO_INSCRITO)),
        "valor_resto_cancelado": parse_decimal(row.get(COL_RESTO_CANCELADO)),
        "valor_resto_pago": parse_decimal(row.get(COL_RESTO_PAGO)),
    }


# ── Reading the artifact ─────────────────────────────────────────────────────
def main_member(members: Iterable[str]) -> str | None:
    """The main table inside the zip (never the Convênios/PorFavorecido siblings)."""
    csvs = [n for n in members if n.lower().endswith(".csv")]
    main = [n for n in csvs if not any(m in n.lower() for m in _SIBLING_MARKERS)]
    return main[0] if main else None


def iter_records(source: Path | str | bytes) -> Iterator[dict[str, str]]:
    """Yield rows of the main CSV from the zip (or from a bare .csv, for fixtures)."""
    if isinstance(source, (str, Path)) and str(source).lower().endswith(".csv"):
        with open(source, encoding=ENCODING, newline="") as fh:
            reader = csv.DictReader(fh, delimiter=DELIMITER)
            validate_header(reader.fieldnames, member=Path(source).name)
            yield from reader
        return

    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = main_member(zf.namelist())
        if member is None:
            raise MissingColumnsError(
                f"no main CSV in the emendas zip; members: {zf.namelist()!r}"
            )
        with zf.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding=ENCODING, newline="")
            reader = csv.DictReader(text, delimiter=DELIMITER)
            validate_header(reader.fieldnames, member=member)
            yield from reader
