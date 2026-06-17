from __future__ import annotations

import datetime as dt

from resumo.util import normalize_name, parse_date, parse_decimal, valid_cpf


def test_normalize_name_strips_accents_and_case():
    assert normalize_name("José da Silva  Júnior") == "JOSE DA SILVA JUNIOR"
    assert normalize_name("#NULO#") is None
    assert normalize_name("  ") is None


def test_valid_cpf_rejects_masked_and_invalid():
    assert valid_cpf("123.456.789-09") == "12345678909"
    assert valid_cpf("***123456**") is None  # TSE mask
    assert valid_cpf("00000000000") is None  # all-equal
    assert valid_cpf("123") is None
    assert valid_cpf(None) is None


def test_parse_date_formats():
    assert parse_date("10/05/1970") == dt.date(1970, 5, 10)
    assert parse_date("1970-05-10") == dt.date(1970, 5, 10)
    assert parse_date("") is None


def test_parse_decimal_brazilian():
    assert parse_decimal("1.234,56") == 1234.56
    assert parse_decimal("1500.00") == 1500.0
    assert parse_decimal("") is None
