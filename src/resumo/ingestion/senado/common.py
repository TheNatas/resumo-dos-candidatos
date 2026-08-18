"""Shared helpers for Senado track-record collectors."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.db.models import House, Mandate


def mandate_map(session: Session, id_legislatura: int) -> dict[str, uuid.UUID]:
    """{house_member_id -> mandate_id} for a legislatura, to attach track-record rows.

    `house_member_id` is `str(CodigoParlamentar)` — the same integer the modern
    endpoints expose as `codigoParlamentar` (votações) and `codSenador` (CEAPS),
    so every Senado source joins on one key.
    """
    rows = session.execute(
        select(Mandate.house_member_id, Mandate.id).where(
            Mandate.house == House.SENADO, Mandate.id_legislatura == id_legislatura
        )
    )
    return {member_id: mid for member_id, mid in rows}
