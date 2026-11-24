"""operation history and runtime events move into the database

Revision ID: b3f8d1c6e2a4
Revises: a7d3e9c1f5b2
Create Date: 2026-11-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3f8d1c6e2a4'
down_revision: Union[str, None] = 'a7d3e9c1f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "ops_history",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=True),
        sa.Column("method_id", sa.String(64), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_ops_history_execution_id", "ops_history", ["execution_id"])
    op.create_index("ix_ops_history_action_id", "ops_history", ["action_id"])
    op.create_index("ix_ops_history_timestamp", "ops_history", ["timestamp"])
    op.create_table(
        "ops_history_labware",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column(
            "record_id", _ID,
            sa.ForeignKey("ops_history.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("labware_name", sa.String(255), nullable=False),
    )
    op.create_index("ix_ops_history_labware_record_id", "ops_history_labware", ["record_id"])
    op.create_index("ix_ops_history_labware_labware_name", "ops_history_labware", ["labware_name"])
    op.create_table(
        "runtime_events",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column("event_name", sa.String(128), nullable=False),
        sa.Column("execution_id", sa.String(64), nullable=True),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(64), nullable=True),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_runtime_events_event_name", "runtime_events", ["event_name"])
    op.create_index("ix_runtime_events_execution_id", "runtime_events", ["execution_id"])


def downgrade() -> None:
    op.drop_table("runtime_events")
    op.drop_table("ops_history_labware")
    op.drop_table("ops_history")
