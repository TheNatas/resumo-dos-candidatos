"""campaign revenue row hash identity

`SQ_RECEITA` was the primary key, but it is not unique: in the 2022/SC file, 72
sequences span 241 extra rows, and the copies are genuinely different money — same
candidate, turno and filing type, but different `VR_RECEITA` and `DS_RECEITA`
(sequence 28316985 carries both R$ 142,50 and R$ 750,00). Keying on it silently
dropped ~0.2% of declared revenue. Identity becomes a row hash, matching the two
despesa families.

Existing `campaign_revenue` rows are therefore **known-incomplete by construction**
and are cleared rather than migrated: there is no way to recover the rows the old
key discarded except by re-reading the source. Re-run
`resumo collect tse-contas --year <ano>` after upgrading (the collector is
idempotent, and its ledger key is scope-aware, so this is a plain re-pull).

`campaign_revenue_originator.sq_receita` loses its FK for the same reason: with a
non-unique sequence on the revenue side it is a join key, not a reference to one row.

Revision ID: 0e3e3de44daf
Revises: d7343ea3ec96
Create Date: 2026-08-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0e3e3de44daf'
down_revision: str | None = 'd7343ea3ec96'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The originator FK must go before the revenue PK it points at.
    op.drop_constraint(
        op.f('campaign_revenue_originator_sq_receita_fkey'),
        'campaign_revenue_originator',
        type_='foreignkey',
    )

    # Incomplete by construction — see the module docstring. Originators are cleared
    # too, since they are only meaningful next to the receipts they qualify.
    op.execute('TRUNCATE campaign_revenue, campaign_revenue_originator')

    # Clearing the rows without clearing the provenance ledger would leave the
    # collector convinced it had already ingested this artifact ("unchanged — hash
    # match"), so the re-pull this migration requires would silently no-op. Any
    # migration that discards collected rows must retract their ledger entries too.
    op.execute("DELETE FROM raw_ingestion WHERE collector_name = 'tse_prestacao_contas'")

    op.drop_constraint(op.f('campaign_revenue_pkey'), 'campaign_revenue', type_='primary')
    op.add_column('campaign_revenue', sa.Column('id', sa.Uuid(), nullable=False))
    op.create_primary_key('campaign_revenue_pkey', 'campaign_revenue', ['id'])

    op.add_column('campaign_revenue', sa.Column('row_hash', sa.String(length=64), nullable=False))
    op.create_unique_constraint('campaign_revenue_row_hash_key', 'campaign_revenue', ['row_hash'])

    op.alter_column('campaign_revenue', 'sq_receita', existing_type=sa.VARCHAR(32), nullable=True)
    op.create_index(
        op.f('ix_campaign_revenue_sq_receita'), 'campaign_revenue', ['sq_receita'], unique=False
    )


def downgrade() -> None:
    # Going back reinstates a key that cannot hold the data, so the table is cleared
    # again rather than left in a state the old constraint would reject.
    op.execute('TRUNCATE campaign_revenue, campaign_revenue_originator')
    op.execute("DELETE FROM raw_ingestion WHERE collector_name = 'tse_prestacao_contas'")

    op.drop_index(op.f('ix_campaign_revenue_sq_receita'), table_name='campaign_revenue')
    op.drop_constraint('campaign_revenue_row_hash_key', 'campaign_revenue', type_='unique')
    op.drop_column('campaign_revenue', 'row_hash')

    op.drop_constraint(op.f('campaign_revenue_pkey'), 'campaign_revenue', type_='primary')
    op.drop_column('campaign_revenue', 'id')
    op.alter_column('campaign_revenue', 'sq_receita', existing_type=sa.VARCHAR(32), nullable=False)
    op.create_primary_key('campaign_revenue_pkey', 'campaign_revenue', ['sq_receita'])

    op.create_foreign_key(
        op.f('campaign_revenue_originator_sq_receita_fkey'),
        'campaign_revenue_originator',
        'campaign_revenue',
        ['sq_receita'],
        ['sq_receita'],
    )
