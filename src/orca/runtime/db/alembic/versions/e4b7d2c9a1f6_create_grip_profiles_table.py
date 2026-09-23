"""create grip_profiles table

Revision ID: e4b7d2c9a1f6
Revises: d5b9e2c8a4f1
Create Date: 2026-08-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4b7d2c9a1f6'
down_revision: Union[str, None] = 'd5b9e2c8a4f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'grip_profiles',
        sa.Column('labware_type', sa.String(length=255), nullable=False),
        sa.Column('patch', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('labware_type'),
    )


def downgrade() -> None:
    op.drop_table('grip_profiles')
