"""create executions table

Revision ID: b7e2c9a4f1d3
Revises: a1d4f8c2b6e9
Create Date: 2026-06-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2c9a4f1d3'
down_revision: Union[str, None] = 'a1d4f8c2b6e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'executions',
        sa.Column('execution_id', sa.String(length=36), nullable=False),
        sa.Column('workflow_name', sa.String(length=255), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('terminal_at', sa.DateTime(), nullable=True),
        sa.Column('terminal_reason', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('execution_id'),
    )


def downgrade() -> None:
    op.drop_table('executions')
