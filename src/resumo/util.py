"""Small normalization helpers shared by collectors and the resolution pipeline."""

from __future__ import annotations

import datetime as dt
import re

from unidecode import unidecode

_WS = re.compile(r"\s+")
_NON_NAME = re.compile(r"[^A-Z0-9 ]")

# TSE sentinels meaning "not informed" — must never be treated as real values.
# The numeric ones are codes, not quantities: `-4` appears in the prestação de contas
# products and would otherwise parse as a real -4.00 currency amount.
_NULLISH = {
    "", "#NULO#", "#NULO", "#NE#", "#NE", "NÃO DIVULGÁVEL", "NAO DIVULGAVEL",
    "-1", "-3", "-4",
}


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


# Conectivos não são iniciais: "JOSÉ DA SILVA" é JS, não JD.
_NAME_PARTICLES = {"DA", "DE", "DO", "DAS", "DOS", "E", "D"}


def initials(value: str | None, *, limit: int = 2) -> str:
    """Up to `limit` initials for a name, for the placeholder shown when a candidacy
    has no official photo. Empty string when there is no usable name — the caller
    renders a blank block rather than a "?" that reads like missing data about the
    person instead of a missing file."""
    words = [w for w in (normalize_name(value) or "").split() if w not in _NAME_PARTICLES]
    if not words:
        return ""
    picked = words if len(words) <= limit else [words[0], *words[-(limit - 1) :]]
    return "".join(w[0] for w in picked)


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


def brl(value: float | int | None) -> str:
    """Format money the way a Brazilian reader expects: ``R$ 926.077,54``.

    The templates used ``'%.2f'|format(...)``, which prints ``926077.54`` — a number
    in a foreign notation, without thousands separators, exactly where the reader most
    needs to see the magnitude at a glance. Six figures and seven figures should not
    look alike.

    Not locale-dependent on purpose: relying on ``locale.setlocale`` would make the
    output depend on which locales the deploy image happens to have installed, and
    the static build and the live app would silently disagree.
    """
    if value is None:
        return "—"
    inteiro, _, centavos = f"{float(value):,.2f}".partition(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"


def ano_range(anos: list[int] | None) -> str | None:
    """``[2025, 2026]`` -> ``"2025–2026"``; ``[2024]`` -> ``"2024"``; ``[]`` -> None.

    Uses an en dash, and says nothing about gaps — a hole in the middle of the range
    is a hole in the source data, and the label that carries this string always names
    the years as *coletados*, never as *cobertos*.
    """
    if not anos:
        return None
    lo, hi = min(anos), max(anos)
    return str(lo) if lo == hi else f"{lo}–{hi}"
