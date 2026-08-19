"""Parse TSE bulk products: zip-of-CSVs, semicolon-delimited, Latin-1 (ISO-8859-1),
plus the zip-of-binaries products (propostas, fotos) at the bottom of the module.

TSE national zips contain one CSV per UF plus (sometimes) a consolidated *_BRASIL
file. To avoid double-counting we prefer the BRASIL file when present, else read
every per-UF CSV. A plain ``.csv`` path is also accepted (handy for fixtures/tests).

When the caller passes ``ufs``, we narrow *before* decoding: the per-UF members are
selected by filename, so a single-state run never pays to parse the other 26 states
(the national candidate zip is ~27x the rows we want). A row-level ``SG_UF`` guard
still runs, covering the case where only a consolidated BRASIL file is published.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

ENCODING = "latin-1"
DELIMITER = ";"

# Per-UF members end in `_SP.csv`; the consolidated one is `_BRASIL.csv` (6 letters),
# so a two-letter class cannot collide with it.
_UF_SUFFIX = re.compile(r"_([A-Za-z]{2})\.csv$")


def member_uf(name: str) -> str | None:
    """The UF a per-UF member file belongs to, or None (BRASIL/unsuffixed)."""
    match = _UF_SUFFIX.search(Path(name).name)
    return match.group(1).upper() if match else None


def _csv_rows(
    text_stream: io.TextIOBase, source_name: str, ufs: frozenset[str] | None
) -> Iterator[dict[str, str]]:
    reader = csv.DictReader(text_stream, delimiter=DELIMITER)
    for row in reader:
        # Row-level guard. Only applied when the product actually carries SG_UF —
        # a missing column must not silently drop every row.
        if ufs and (uf := row.get("SG_UF")) is not None and uf.strip().upper() not in ufs:
            continue
        # Stash the originating member name for proposta<->candidate mapping etc.
        row["__source_file"] = source_name
        yield row


def _select_members(members: list[str], ufs: frozenset[str] | None, prefer_brasil: bool) -> list[str]:
    """Pick which CSV members to read, avoiding double-counting.

    With a UF filter, per-UF members win over the consolidated BRASIL file: reading
    both would duplicate every row, and the per-UF files are the cheaper path.
    """
    if ufs:
        per_uf = [n for n in members if member_uf(n) in ufs]
        if per_uf:
            return per_uf
        # No per-UF member for the requested states — fall back to whatever exists
        # and let the row-level SG_UF guard do the narrowing.
    brasil = [n for n in members if "brasil" in n.lower()]
    return brasil if (prefer_brasil and brasil) else members


def iter_records(
    source: Path | str | bytes,
    *,
    prefer_brasil: bool = True,
    ufs: Iterable[str] | None = None,
) -> Iterator[dict[str, str]]:
    """Yield CSV rows (as dicts) from a TSE zip, raw bytes, or a single .csv path.

    `ufs` restricts to the given states (by member filename, then by SG_UF); None or
    empty means no geographic filter.
    """
    uf_set = frozenset(u.strip().upper() for u in ufs if u.strip()) if ufs else None

    if isinstance(source, (str, Path)) and str(source).lower().endswith(".csv"):
        with open(source, encoding=ENCODING, newline="") as fh:
            yield from _csv_rows(fh, Path(source).name, uf_set)
        return

    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        for name in _select_members(members, uf_set, prefer_brasil):
            with zf.open(name) as raw:
                text = io.TextIOWrapper(raw, encoding=ENCODING, newline="")
                yield from _csv_rows(text, name, uf_set)


# The bulk zips of *binary* products (propostas, fotos) carry no manifest: the only
# link back to a candidacy is the digit run in the member path. 10+ digits so a
# year or a page number can never be read as an SQ_CANDIDATO (real ones are ~12).
_DIGIT_RUN = re.compile(r"\d{10,}")


def match_sq(filename: str, known: set[str]) -> str | None:
    """The SQ_CANDIDATO a binary member belongs to, or None when no digit run in the
    path is a candidacy we hold. Never guesses: an unmatched file stays unattributed."""
    for run in _DIGIT_RUN.findall(filename):
        if run in known:
            return run
    return None


def _members_with_suffix(source: Path | str | bytes, suffixes: tuple[str, ...]) -> list[str]:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return [n for n in zf.namelist() if n.lower().endswith(suffixes)]


def list_pdf_members(source: Path | str | bytes) -> list[str]:
    return _members_with_suffix(source, (".pdf",))


def list_image_members(source: Path | str | bytes) -> list[str]:
    """TSE ships the photo bundles as JPEG, but the extension is not contractual —
    accept the other still formats rather than silently publishing nobody's face."""
    return _members_with_suffix(source, (".jpg", ".jpeg", ".png", ".webp"))


def read_member(source: Path | str | bytes, member: str) -> bytes:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(member)
