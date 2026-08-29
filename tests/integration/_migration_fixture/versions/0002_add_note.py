"""add note column

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("widgets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("note", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("widgets", schema=None) as batch_op:
        batch_op.drop_column("note")
