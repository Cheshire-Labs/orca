"""move_defaults row holds a sparse patch

Revision ID: d5b9e2c8a4f1
Revises: c3a8f5b1e7d2
Create Date: 2026-08-22 00:00:00.000000

The column now holds only the fields somebody set, so a field nobody touched
follows the seed instead of freezing at what the seed said when the row was
written. A total record written by the old shape reads back as a patch that
names every field, which resolves to the same numbers, so no data moves.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd5b9e2c8a4f1'
down_revision: Union[str, None] = 'c3a8f5b1e7d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('move_defaults', 'parameters', new_column_name='patch')


def downgrade() -> None:
    op.alter_column('move_defaults', 'patch', new_column_name='parameters')
