"""create move_defaults table

Revision ID: c3a8f5b1e7d2
Revises: b7e2c9a4f1d3
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3a8f5b1e7d2'
down_revision: Union[str, None] = 'b7e2c9a4f1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'move_defaults',
        sa.Column('transporter_name', sa.String(length=255), nullable=False),
        sa.Column('parameters', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('transporter_name'),
    )


def downgrade() -> None:
    op.drop_table('move_defaults')
