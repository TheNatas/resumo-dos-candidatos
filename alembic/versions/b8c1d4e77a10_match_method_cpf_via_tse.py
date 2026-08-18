"""match_method cpf_via_tse

Revision ID: b8c1d4e77a10
Revises: 0e3e3de44daf
Create Date: 2026-08-18

CPF recuperado pelo histórico do TSE para Casas que não publicam CPF. Valor novo no
enum nativo do Postgres — precisa de ALTER TYPE, não basta mudar o Python.
"""

from alembic import op

revision = "b8c1d4e77a10"
down_revision = "0e3e3de44daf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE matchmethod ADD VALUE IF NOT EXISTS 'cpf_via_tse'")


def downgrade() -> None:
    # O Postgres não remove valor de enum. Reverter exigiria recriar o tipo e
    # reescrever a coluna; como o valor extra é inerte para quem não o usa, a
    # descida é um no-op consciente em vez de uma cirurgia arriscada.
    pass
