"""Small normalization helpers shared by collectors and the resolution pipeline."""

from __future__ import annotations

import datetime as dt
import re

from unidecode import unidecode

_WS = re.compile(r"\s+")
_NON_NAME = re.compile(r"[^A-Z0-9 ]")

# TSE sentinels meaning "not informed" — must never be treated as real values.
_NULLISH = {"", "#NULO#", "#NULO", "#NE#", "NÃO DIVULGÁVEL", "NAO DIVULGAVEL", "-1", "-3"}


def clean(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        # JSON APIs (Câmara) deliver native ints/floats; coerce before cleaning.
        value = str(value)
    v = value.strip()
    if v.upper() in _NULLISH or v in _NULLISH:
        return None
    return v


def normalize_name(value: str | None) -> str | None:
    """Uppercase, accent-stripped, single-spaced — the blocking/match key."""
    v = clean(value)
    if v is None:
        return None
    v = unidecode(v).upper()
    v = _NON_NAME.sub(" ", v)
    v = _WS.sub(" ", v).strip()
    return v or None


def only_digits(value: str | None) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def valid_cpf(value: str | None) -> str | None:
    """Return an 11-digit CPF if `value` looks like a real (non-masked) CPF, else None.

    TSE masks CPF as e.g. ``***123456**`` (non-digits) or zero-fills it; those are
    rejected so they never seed a false deterministic match.
    """
    digits = only_digits(value)
    if not digits or len(digits) != 11:
        return None
    if len(set(digits)) == 1:  # 00000000000, 11111111111, ...
        return None
    return digits


def parse_date(value: str | None) -> dt.date | None:
    v = clean(value)
    if v is None:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(v[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    return None


def parse_decimal(value: str | None) -> float | None:
    """Parse Brazilian-formatted decimals: '1.234,56' -> 1234.56."""
    v = clean(value)
    if v is None:
        return None
    v = v.replace(".", "").replace(",", ".") if "," in v else v
    try:
        return float(v)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    v = clean(value)
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None
