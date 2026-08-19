"""candidate_photo

Revision ID: c7d4a9f1e2b3
Revises: b8c1d4e77a10
Create Date: 2026-08-19

Foto oficial de registro, do pacote `foto_cand<ano>_<UF>_div.zip` do TSE.

Chave primária é o próprio `sq_candidato`: uma candidatura tem exatamente UMA foto
de registro, então uma foto reemitida substitui a anterior em vez de conviver com
ela — o oposto de `government_proposal`, onde várias linhas por candidatura são
legítimas e a chave inclui o hash.
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d4a9f1e2b3"
down_revision = "b8c1d4e77a10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidate_photo",
        sa.Column("sq_candidato", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["sq_candidato"], ["candidacy.sq_candidato"]),
        sa.PrimaryKeyConstraint("sq_candidato"),
    )


def downgrade() -> None:
    op.drop_table("candidate_photo")
