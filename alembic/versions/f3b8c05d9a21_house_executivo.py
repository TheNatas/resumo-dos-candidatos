"""add EXECUTIVO to the house enum

Widens `Mandate.house` (and every other column on the `house` enum type) so a sitting
governor can hold a mandate row. Value-only change: no table is rewritten.

Unlike ASSEMBLEIA, this value is NOT another legislature — see `House.EXECUTIVO` for
what an executive mandate does and does not carry. Nothing in the schema needed to
change for that: `Vote`, `AttendanceRecord`, `MandateLeave` and `Expense` all hang off
`Mandate` by nullable FK and simply stay empty for an executive term.

Revision ID: f3b8c05d9a21
Revises: e5a2b8c31f47
Create Date: 2026-08-28
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'f3b8c05d9a21'
down_revision: str | None = 'e5a2b8c31f47'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this safe to re-run against a DB created from ORM metadata
    # (the test database builds the type with every value already present).
    op.execute("ALTER TYPE house ADD VALUE IF NOT EXISTS 'EXECUTIVO'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type. Removing it would mean
    # recreating the type and rewriting every dependent column — not worth doing
    # automatically, and a no-op downgrade leaves the schema strictly compatible.
    pass
