"""setup moves to the package's files; plate positions move into the database

Teachpoints, deck layouts, access configs, move defaults, grip profiles
and added labware are files in the deployment package, so their tables go.
Plate identities and their history are activity, so they get tables here, as
do the per-thread run summaries, which the models declared with no migration.

Revision ID: 8e1f4c7a2b90
Revises: f6c3a8e5d2b7
Create Date: 2026-11-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8e1f4c7a2b90'
down_revision: Union[str, None] = 'f6c3a8e5d2b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SETUP_TABLES = (
    "teachpoints", "deck_layouts", "labware_definitions",
    "access_configs", "move_defaults", "grip_profiles",
)


def upgrade() -> None:
    for table in _SETUP_TABLES:
        op.drop_table(table)
    op.create_table(
        "execution_threads",
        sa.Column("execution_id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("template_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("pause_reason", sa.String(64), nullable=True),
    )
    op.create_table(
        "labware_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("template_name", sa.String(255), nullable=False),
        sa.Column("labware_type", sa.String(255), nullable=False),
        sa.Column("barcode", sa.String(255), nullable=True),
        sa.Column("position_id", sa.String(255), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("carry_override", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("barcode", name="uix_labware_barcode"),
    )
    op.create_index("ix_labware_instances_template_name", "labware_instances", ["template_name"])
    op.create_index("ix_labware_instances_barcode", "labware_instances", ["barcode"])
    op.create_index("ix_labware_instances_execution_id", "labware_instances", ["execution_id"])
    op.create_table(
        "labware_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column(
            "labware_id", sa.String(36),
            sa.ForeignKey("labware_instances.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_labware_events_labware_id", "labware_events", ["labware_id"])
    op.create_index("ix_labware_events_event_type", "labware_events", ["event_type"])
    op.create_index("ix_labware_events_ts", "labware_events", ["ts"])


def downgrade() -> None:
    raise NotImplementedError(
        "setup lives in the deployment package's files; there is no way back "
        "to setup tables",
    )
