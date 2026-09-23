"""teachpoint records what it was taught with, and per-labware overrides

Layer three of the move-parameter model. ``taught_with`` names the labware the
position was jogged to, which is what ``z_offset`` is relative to. ``by_labware``
holds sparse per-labware patches for this one position: the narrowest layer
there is, for a nest that suits every labware except one.

Both are nullable, so every existing teachpoint keeps resolving exactly as it
did.

Revision ID: f6c3a8e5d2b7
Revises: e4b7d2c9a1f6
Create Date: 2026-08-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6c3a8e5d2b7'
down_revision: Union[str, None] = 'e4b7d2c9a1f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'teachpoints',
        sa.Column('taught_with', sa.String(length=255), nullable=True),
    )
    op.add_column(
        'teachpoints',
        sa.Column('by_labware', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('teachpoints', 'by_labware')
    op.drop_column('teachpoints', 'taught_with')
