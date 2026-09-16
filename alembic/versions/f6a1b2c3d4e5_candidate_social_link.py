"""candidate social links from the TSE bulk product."""

import sqlalchemy as sa
from alembic import op

revision = "f6a1b2c3d4e5"
down_revision = "e5a2b8c31f47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidate_social_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sq_candidato", sa.String(length=32), nullable=False),
        sa.Column("network", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.ForeignKeyConstraint(["sq_candidato"], ["candidacy.sq_candidato"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sq_candidato", "network", "url"),
    )
    op.create_index("ix_candidate_social_link_sq_candidato", "candidate_social_link", ["sq_candidato"])


def downgrade() -> None:
    op.drop_index("ix_candidate_social_link_sq_candidato", table_name="candidate_social_link")
    op.drop_table("candidate_social_link")