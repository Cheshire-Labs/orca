"""operation history is indexed by device

Revision ID: d9a2c7e4b1f8
Revises: b3f8d1c6e2a4
Create Date: 2026-11-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pydantic import JsonValue


revision: str = 'd9a2c7e4b1f8'
down_revision: Union[str, None] = 'b3f8d1c6e2a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_OPS_HISTORY = sa.table("ops_history", sa.column("id", _ID), sa.column("payload", sa.JSON()))
_OPS_HISTORY_DEVICE = sa.table(
    "ops_history_device", sa.column("record_id", _ID), sa.column("device_name", sa.String()),
)


def upgrade() -> None:
    op.create_table(
        "ops_history_device",
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column(
            "record_id", _ID,
            sa.ForeignKey("ops_history.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("device_name", sa.String(255), nullable=False),
    )
    op.create_index("ix_ops_history_device_record_id", "ops_history_device", ["record_id"])
    op.create_index("ix_ops_history_device_device_name", "ops_history_device", ["device_name"])

    bind = op.get_bind()
    pairs: list[dict[str, JsonValue]] = []
    for record_id, payload in bind.execute(sa.select(_OPS_HISTORY.c.id, _OPS_HISTORY.c.payload)):
        pairs.extend(
            {"record_id": record_id, "device_name": name} for name in _device_names(payload)
        )
    if pairs:
        bind.execute(sa.insert(_OPS_HISTORY_DEVICE), pairs)


def _device_names(payload: JsonValue) -> list[str]:
    if not isinstance(payload, dict):
        return []
    operations = payload.get("operations")
    if not isinstance(operations, list):
        return []
    names: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        name = operation.get("device_name")
        if isinstance(name, str) and name:
            names.add(name)
    return sorted(names)


def downgrade() -> None:
    op.drop_table("ops_history_device")
