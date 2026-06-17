"""Parse TSE bulk products: zip-of-CSVs, semicolon-delimited, Latin-1 (ISO-8859-1).

TSE national zips contain one CSV per UF plus (sometimes) a consolidated *_BRASIL
file. To avoid double-counting we prefer the BRASIL file when present, else read
every per-UF CSV. A plain ``.csv`` path is also accepted (handy for fixtures/tests).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

ENCODING = "latin-1"
DELIMITER = ";"


def _csv_rows(text_stream: io.TextIOBase, source_name: str) -> Iterator[dict[str, str]]:
    reader = csv.DictReader(text_stream, delimiter=DELIMITER)
    for row in reader:
        # Stash the originating member name for proposta<->candidate mapping etc.
        row["__source_file"] = source_name
        yield row


def iter_records(source: Path | str | bytes, *, prefer_brasil: bool = True) -> Iterator[dict[str, str]]:
    """Yield CSV rows (as dicts) from a TSE zip, raw bytes, or a single .csv path."""
    if isinstance(source, (str, Path)) and str(source).lower().endswith(".csv"):
        with open(source, encoding=ENCODING, newline="") as fh:
            yield from _csv_rows(fh, Path(source).name)
        return

    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        brasil = [n for n in members if "brasil" in n.lower()]
        chosen = brasil if (prefer_brasil and brasil) else members
        for name in chosen:
            with zf.open(name) as raw:
                text = io.TextIOWrapper(raw, encoding=ENCODING, newline="")
                yield from _csv_rows(text, name)


def list_pdf_members(source: Path | str | bytes) -> list[str]:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return [n for n in zf.namelist() if n.lower().endswith(".pdf")]


def read_member(source: Path | str | bytes, member: str) -> bytes:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(member)
