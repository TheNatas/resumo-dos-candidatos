"""official party proposals

Revision ID: 2f4a9b8c7d61
Revises: f6a1b2c3d4e5
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2f4a9b8c7d61"
down_revision = "f6a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "party_proposal",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_sigla", sa.String(length=32), nullable=False),
        sa.Column("ano_eleicao", sa.Integer(), nullable=False),
        sa.Column("uf", sa.String(length=2), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=False),
        sa.Column("verification_method", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("storage_path", sa.String(length=512), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("party_sigla", "ano_eleicao", "uf", "content_hash"),
    )
    op.create_index("ix_party_proposal_party_sigla", "party_proposal", ["party_sigla"])
    op.create_index("ix_party_proposal_ano_eleicao", "party_proposal", ["ano_eleicao"])
    op.create_index("ix_party_proposal_uf", "party_proposal", ["uf"])


def downgrade() -> None:
    op.drop_index("ix_party_proposal_uf", table_name="party_proposal")
    op.drop_index("ix_party_proposal_ano_eleicao", table_name="party_proposal")
    op.drop_index("ix_party_proposal_party_sigla", table_name="party_proposal")
    op.drop_table("party_proposal")