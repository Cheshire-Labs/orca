"""each device's sim or live setting

Revision ID: c8e1f4a7b2d9
Revises: d9a2c7e4b1f8
Create Date: 2026-11-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8e1f4a7b2d9'
down_revision: Union[str, None] = 'd9a2c7e4b1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_modes",
        sa.Column("device_name", sa.String(255), primary_key=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("device_modes")
