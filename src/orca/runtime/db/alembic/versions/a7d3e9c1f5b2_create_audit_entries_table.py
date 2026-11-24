"""audit rows move into the database

Revision ID: a7d3e9c1f5b2
Revises: 8e1f4c7a2b90
Create Date: 2026-11-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7d3e9c1f5b2'
down_revision: Union[str, None] = '8e1f4c7a2b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_entries",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("action_name", sa.String(128), nullable=False),
        sa.Column("danger_level", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("call_args", sa.JSON(), nullable=False),
    )
    op.create_index("ix_audit_entries_timestamp", "audit_entries", ["timestamp"])
    op.create_index("ix_audit_entries_action_name", "audit_entries", ["action_name"])
    op.create_index("ix_audit_entries_danger_level", "audit_entries", ["danger_level"])


def downgrade() -> None:
    op.drop_table("audit_entries")
