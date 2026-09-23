"""store the gender declared in the TSE candidacy export"""

import sqlalchemy as sa
from alembic import op

revision = "8c2d4e5f6a70"
down_revision = "3a5b7c9d1e2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidacy", sa.Column("genero", sa.String(length=32), nullable=True))
    op.create_index("ix_candidacy_genero", "candidacy", ["genero"])


def downgrade() -> None:
    op.drop_index("ix_candidacy_genero", table_name="candidacy")
    op.drop_column("candidacy", "genero")