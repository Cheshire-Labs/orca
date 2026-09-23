"""create labware_definitions table

Revision ID: a1d4f8c2b6e9
Revises: 9f3d1e7a2c4b
Create Date: 2026-06-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1d4f8c2b6e9'
down_revision: Union[str, None] = '9f3d1e7a2c4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'labware_definitions',
        sa.Column('labware_type', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('vendor', sa.String(length=255), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('geometry', sa.JSON(), nullable=False),
        sa.Column('plr_class_name', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('labware_type'),
    )


def downgrade() -> None:
    op.drop_table('labware_definitions')
