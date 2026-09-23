"""create access_configs table

Revision ID: 4b1c9a2d6e3f
Revises: 2859ba3c7f65
Create Date: 2026-06-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4b1c9a2d6e3f'
down_revision: Union[str, None] = '2859ba3c7f65'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'access_configs',
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('access_type', sa.String(length=16), nullable=False),
        sa.Column('gripper_offset', sa.Float(), nullable=False),
        sa.Column('vertical_clearance', sa.Float(), nullable=False),
        sa.Column('horizontal_clearance', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('access_configs')
