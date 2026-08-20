"""attendance_summary + mandate_leave

Revision ID: e5a2b8c31f47
Revises: c7d4a9f1e2b3
Create Date: 2026-08-19

Frequência consolidada com o denominador junto, e licenças formais.

`attendance_record` tem grão de evento e não responde "quantos dias faltou": o
denominador ali é *o que foi coletado*, não *o que era devido*. `attendance_summary`
guarda o consolidado com o universo esperado — e uma linha **por unidade**, porque a
Câmara publica as duas réguas (dias com sessão deliberativa E sessões deliberativas
com Ordem do Dia iniciada) e elas não batem entre si de propósito. Converter uma na
outra seria inventar o que a fonte não diz, então a chave única inclui `unidade`.

`mandate_leave` fica separada: licença é medida em dias corridos de calendário, régua
que não se soma à de sessões.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a2b8c31f47"
down_revision: str | None = "c7d4a9f1e2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Os tipos enum são referenciados com `create_type=False` (dialeto postgres): o
# `house` já existe desde o schema inicial, e `attendanceunit` é criado uma única
# vez logo abaixo — sem isso, cada coluna tentaria recriar o tipo e a segunda falha.
_UNIT = postgresql.ENUM("SESSAO", "DIA", name="attendanceunit", create_type=False)
_HOUSE = postgresql.ENUM(
    "CAMARA", "SENADO", "ASSEMBLEIA", name="house", create_type=False
)


def upgrade() -> None:
    _UNIT.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "attendance_summary",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mandate_id", sa.Uuid(), nullable=False),
        sa.Column("house", _HOUSE, nullable=False),
        sa.Column("house_member_id", sa.String(length=32), nullable=False),
        sa.Column("ano", sa.Integer(), nullable=False),
        sa.Column("ambito", sa.String(length=16), nullable=False),
        sa.Column("unidade", _UNIT, nullable=False),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("presenca", sa.Integer(), nullable=True),
        sa.Column("ausencia_justificada", sa.Integer(), nullable=True),
        sa.Column("ausencia_nao_justificada", sa.Integer(), nullable=True),
        sa.Column("ausencia_nao_classificada", sa.Integer(), nullable=True),
        sa.Column("metrica", sa.String(length=64), nullable=False),
        sa.Column("derivation", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(["mandate_id"], ["mandate.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mandate_id", "ano", "ambito", "unidade"),
    )
    op.create_index(
        op.f("ix_attendance_summary_mandate_id"), "attendance_summary", ["mandate_id"]
    )
    op.create_index(op.f("ix_attendance_summary_house"), "attendance_summary", ["house"])
    op.create_index(
        op.f("ix_attendance_summary_house_member_id"), "attendance_summary", ["house_member_id"]
    )
    op.create_index(op.f("ix_attendance_summary_ano"), "attendance_summary", ["ano"])
    op.create_index(op.f("ix_attendance_summary_metrica"), "attendance_summary", ["metrica"])

    op.create_table(
        "mandate_leave",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mandate_id", sa.Uuid(), nullable=False),
        sa.Column("house", _HOUSE, nullable=False),
        sa.Column("house_member_id", sa.String(length=32), nullable=False),
        sa.Column("leave_id", sa.String(length=32), nullable=False),
        sa.Column("data_inicio", sa.Date(), nullable=True),
        sa.Column("data_fim", sa.Date(), nullable=True),
        sa.Column("sigla_tipo", sa.String(length=64), nullable=True),
        sa.Column("descricao_tipo", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["mandate_id"], ["mandate.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mandate_id", "leave_id"),
    )
    op.create_index(op.f("ix_mandate_leave_mandate_id"), "mandate_leave", ["mandate_id"])
    op.create_index(
        op.f("ix_mandate_leave_house_member_id"), "mandate_leave", ["house_member_id"]
    )


def downgrade() -> None:
    op.drop_table("mandate_leave")
    op.drop_table("attendance_summary")
    _UNIT.drop(op.get_bind(), checkfirst=True)
