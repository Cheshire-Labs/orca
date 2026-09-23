"""create teachpoints table

Revision ID: 7c2e4f1a9b8d
Revises: 4b1c9a2d6e3f
Create Date: 2026-06-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c2e4f1a9b8d'
down_revision: Union[str, None] = '4b1c9a2d6e3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'teachpoints',
        sa.Column('position_id', sa.String(length=255), nullable=False),
        sa.Column('coord_type', sa.String(length=16), nullable=True),
        sa.Column('coords', sa.JSON(), nullable=True),
        sa.Column('orientation', sa.String(length=16), nullable=True),
        sa.Column('gateway', sa.String(length=255), nullable=True),
        sa.Column('access_config_name', sa.String(length=255), nullable=True),
        sa.Column('access_type', sa.String(length=16), nullable=True),
        sa.Column('gripper_offset', sa.Float(), nullable=True),
        sa.Column('vertical_clearance', sa.Float(), nullable=True),
        sa.Column('horizontal_clearance', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('position_id'),
    )


def downgrade() -> None:
    op.drop_table('teachpoints')
