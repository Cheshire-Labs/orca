"""executions is indexed by status, submission time and workflow name

Revision ID: e7a3c5d1f9b2
Revises: c8e1f4a7b2d9
Create Date: 2026-11-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'e7a3c5d1f9b2'
down_revision: Union[str, None] = 'c8e1f4a7b2d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = {
    'ix_executions_status': 'status',
    'ix_executions_submitted_at': 'submitted_at',
    'ix_executions_workflow_name': 'workflow_name',
}


def upgrade() -> None:
    for name, column in _INDEXES.items():
        op.create_index(name, 'executions', [column])


def downgrade() -> None:
    for name in _INDEXES:
        op.drop_index(name, table_name='executions')
