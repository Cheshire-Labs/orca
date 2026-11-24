"""execution_threads carries the labware group each thread serves

Nullable: a thread shared across groups serves none, and existing rows keep
reading as before.

Revision ID: a4c9e2f7b1d3
Revises: e7a3c5d1f9b2
Create Date: 2026-11-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4c9e2f7b1d3'
down_revision: Union[str, None] = 'e7a3c5d1f9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('execution_threads', sa.Column('group_id', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('execution_threads', 'group_id')
