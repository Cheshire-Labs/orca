"""The operation record's rows and query conditions, for any database.

Every record's full payload is in ``ops_history.payload``. ``ops_history_labware``
and ``ops_history_device`` hold one row per record and labware, and per record and
device, so a search by either narrows in SQL. These are builders, not calls: a
store runs them on a blocking or an async session.
"""

from collections.abc import Sequence
from typing import List, Tuple

from sqlalchemy import ColumnElement, select

from orca.runtime.db.models import OpsHistoryDeviceRow, OpsHistoryLabwareRow, OpsHistoryRow
from orca.state.ops_store import OpsHistorySearchQuery, record_matches
from orca.state.records import TrackingRecord


def record_row(execution_id: str, record: TrackingRecord) -> OpsHistoryRow:
    return OpsHistoryRow(
        execution_id=execution_id,
        action_id=record.action_id,
        thread_id=record.thread_id or None,
        method_id=record.method_id or None,
        source=record.source.value,
        timestamp=record.timestamp,
        payload=record.model_dump(mode="json"),
    )


def labware_rows(record_id: int, record: TrackingRecord) -> list[OpsHistoryLabwareRow]:
    names = sorted({name for op in record.operations for name in op.affected_labware})
    return [OpsHistoryLabwareRow(record_id=record_id, labware_name=name) for name in names]


def device_rows(record_id: int, record: TrackingRecord) -> list[OpsHistoryDeviceRow]:
    names = sorted({op.device_name for op in record.operations if op.device_name})
    return [OpsHistoryDeviceRow(record_id=record_id, device_name=name) for name in names]


# Each store spells its own select: the SQLite store runs a blocking session and the other an async
# one, and a returned Select is typed differently by SQLAlchemy 2.0 and 2.1, which are both allowed.
LIST_ORDER = (OpsHistoryRow.id.asc(),)
SEARCH_ORDER = (OpsHistoryRow.timestamp.asc(), OpsHistoryRow.id.asc())


def list_conditions(execution_id: str) -> list[ColumnElement[bool]]:
    """One execution's records. Read in ``LIST_ORDER``, the order they were written."""
    return [OpsHistoryRow.execution_id == execution_id]


def search_conditions(query: OpsHistorySearchQuery) -> list[ColumnElement[bool]]:
    """Rows that can match the query. Read in ``SEARCH_ORDER``: timestamp, ties in write order.

    Labware and device narrow in SQL because contents and head reads search by them.
    Operation is matched on the parsed records by ``search_matches``.
    """
    conditions: list[ColumnElement[bool]] = []
    if query.execution_id is not None:
        conditions.append(OpsHistoryRow.execution_id == query.execution_id)
    if query.action_id is not None:
        conditions.append(OpsHistoryRow.action_id == query.action_id)
    if query.thread_id is not None:
        conditions.append(OpsHistoryRow.thread_id == query.thread_id)
    if query.method_id is not None:
        conditions.append(OpsHistoryRow.method_id == query.method_id)
    if query.source is not None:
        conditions.append(OpsHistoryRow.source == query.source.value)
    if query.labware_name is not None:
        conditions.append(
            select(OpsHistoryLabwareRow.id)
            .where(
                OpsHistoryLabwareRow.record_id == OpsHistoryRow.id,
                OpsHistoryLabwareRow.labware_name == query.labware_name,
            )
            .exists()
        )
    if query.device_name is not None:
        conditions.append(
            select(OpsHistoryDeviceRow.id)
            .where(
                OpsHistoryDeviceRow.record_id == OpsHistoryRow.id,
                OpsHistoryDeviceRow.device_name == query.device_name,
            )
            .exists()
        )
    return conditions


def records(rows: Sequence[OpsHistoryRow]) -> List[TrackingRecord]:
    return [TrackingRecord.model_validate(row.payload) for row in rows]


def search_matches(
    rows: Sequence[OpsHistoryRow], query: OpsHistorySearchQuery,
) -> List[Tuple[str, TrackingRecord]]:
    """Each row's execution id and record, for the records the whole query matches."""
    matches: List[Tuple[str, TrackingRecord]] = []
    for row in rows:
        record = TrackingRecord.model_validate(row.payload)
        if record_matches(record, query):
            matches.append((row.execution_id, record))
    return matches
