"""merge schema heads after social links and executive house changes."""

from collections.abc import Sequence

revision: str = "3a5b7c9d1e2f"
down_revision: tuple[str, str] = ("2f4a9b8c7d61", "f3b8c05d9a21")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
