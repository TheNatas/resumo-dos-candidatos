"""add ASSEMBLEIA to the house enum

Widens `Mandate.house` (and every other column on the `house` enum type) so state
assemblies can hold mandates alongside CAMARA and SENADO. Value-only change: no
table is rewritten.

Revision ID: a1c4f7e21b05
Revises: d9f2937618b0
Create Date: 2026-08-18
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'a1c4f7e21b05'
down_revision: str | None = 'd9f2937618b0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this safe to re-run against a DB created from ORM metadata
    # (the test database builds the type with every value already present).
    op.execute("ALTER TYPE house ADD VALUE IF NOT EXISTS 'ASSEMBLEIA'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type. Removing it would mean
    # recreating the type and rewriting every dependent column — not worth doing
    # automatically, and a no-op downgrade leaves the schema strictly compatible.
    pass
