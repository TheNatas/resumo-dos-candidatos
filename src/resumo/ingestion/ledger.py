"""Provenance + idempotency helpers shared by every collector.

The core idempotency contract: hash the fetched artifact, and if we already
ingested that exact (source_url, content_hash) successfully, skip re-normalizing.
This makes scheduled re-pulls cheap no-ops when the upstream file is unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from resumo.db.models import RawIngestion
from resumo.db.session import Base


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def already_ingested(session: Session, source_url: str, digest: str) -> bool:
    """True if this exact artifact was previously ingested successfully."""
    stmt = (
        select(RawIngestion.id)
        .where(
            RawIngestion.source_url == source_url,
            RawIngestion.content_hash == digest,
            RawIngestion.status == "success",
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def record_ingestion(
    session: Session,
    *,
    collector_name: str,
    source_url: str,
    digest: str,
    row_count: int,
    source_generated_at: str | None = None,
    status: str = "success",
) -> RawIngestion:
    row = RawIngestion(
        collector_name=collector_name,
        source_url=source_url,
        content_hash=digest,
        row_count=row_count,
        source_generated_at=source_generated_at,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def upsert(
    session: Session,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    *,
    index_elements: Iterable[str],
    update_columns: Iterable[str] | None = None,
    batch_size: int = 1000,
) -> int:
    """Idempotent bulk upsert via Postgres ON CONFLICT DO UPDATE.

    `index_elements` is the natural/unique key. `update_columns` defaults to every
    inserted column except the conflict key (so re-ingesting refreshes mutable fields).
    Returns the number of rows processed.
    """
    rows = [r for r in rows if r]
    if not rows:
        return 0

    index_elements = list(index_elements)

    # Collapse duplicates on the conflict key (last wins). Postgres forbids a row
    # being affected twice by one ON CONFLICT command, and some sources (e.g. CEAP)
    # legitimately repeat keys within a single pull.
    deduped: dict[tuple, dict] = {}
    for r in rows:
        deduped[tuple(r.get(k) for k in index_elements)] = r
    rows = list(deduped.values())

    total = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        stmt = pg_insert(model).values(chunk)
        cols = (
            list(update_columns)
            if update_columns is not None
            else [c for c in chunk[0].keys() if c not in index_elements]
        )
        if cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={c: getattr(stmt.excluded, c) for c in cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=index_elements)
        session.execute(stmt)
        total += len(chunk)
    return total
