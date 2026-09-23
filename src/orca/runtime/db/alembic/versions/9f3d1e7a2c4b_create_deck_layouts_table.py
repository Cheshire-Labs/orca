"""create deck_layouts table

Revision ID: 9f3d1e7a2c4b
Revises: 7c2e4f1a9b8d
Create Date: 2026-06-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9f3d1e7a2c4b'
down_revision: Union[str, None] = '7c2e4f1a9b8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deck_layouts',
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('deck_data', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('deck_layouts')
