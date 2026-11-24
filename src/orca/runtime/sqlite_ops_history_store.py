import asyncio
from typing import List, Tuple

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from orca.runtime.db import ops_history_queries
from orca.runtime.db.models import OpsHistoryDeviceRow, OpsHistoryRow
from orca.state.ops_store import (
    IOpsHistoryStore,
    IOpsHistoryView,
    OpsHistorySearchQuery,
    StoreBackedOpsHistoryView,
)
from orca.state.records import TrackingRecord


class SqliteOpsHistoryStore(IOpsHistoryStore):
    """The operation record in the SQLite activity database. The server creates its tables.

    Every device command appends here. The queries run on the loop's default
    executor, which the loop drains before it closes, so an append in flight at
    shutdown finishes instead of answering a closed loop. The rows and query
    conditions are in ``orca.runtime.db.ops_history_queries``.
    """

    def __init__(self, engine: Engine) -> None:
        self._sf = sessionmaker(engine, class_=Session, expire_on_commit=False)

    async def append(self, execution_id: str, record: TrackingRecord) -> None:
        await asyncio.to_thread(self._append, execution_id, record)

    async def index_old_records_by_device(self) -> None:
        """Add device rows for records saved before the device index existed."""
        await asyncio.to_thread(self._index_old_records_by_device)

    def for_execution(self, execution_id: str) -> IOpsHistoryView:
        return StoreBackedOpsHistoryView(self, execution_id)

    async def list(self, execution_id: str) -> List[TrackingRecord]:
        return await asyncio.to_thread(self._list, execution_id)

    async def search(self, query: OpsHistorySearchQuery) -> List[Tuple[str, TrackingRecord]]:
        return await asyncio.to_thread(self._search, query)

    def _append(self, execution_id: str, record: TrackingRecord) -> None:
        with self._sf() as session:
            row = ops_history_queries.record_row(execution_id, record)
            session.add(row)
            # The id is the database's, so the labware and device rows can only be built after the insert.
            session.flush()
            session.add_all(ops_history_queries.labware_rows(row.id, record))
            session.add_all(ops_history_queries.device_rows(row.id, record))
            session.commit()

    def _index_old_records_by_device(self) -> None:
        has_device_rows = select(OpsHistoryDeviceRow.id).where(OpsHistoryDeviceRow.record_id == OpsHistoryRow.id)
        unindexed = select(OpsHistoryRow.id, OpsHistoryRow.payload).where(~has_device_rows.exists())
        with self._sf() as session:
            found: list[OpsHistoryDeviceRow] = []
            for record_id, payload in session.execute(unindexed).yield_per(500):
                found.extend(ops_history_queries.device_rows(record_id, TrackingRecord.model_validate(payload)))
            session.add_all(found)
            session.commit()

    def _list(self, execution_id: str) -> List[TrackingRecord]:
        with self._sf() as session:
            stmt = (
                select(OpsHistoryRow)
                .where(*ops_history_queries.list_conditions(execution_id))
                .order_by(*ops_history_queries.LIST_ORDER)
            )
            rows = session.execute(stmt).scalars().all()
            return ops_history_queries.records(rows)

    def _search(self, query: OpsHistorySearchQuery) -> List[Tuple[str, TrackingRecord]]:
        with self._sf() as session:
            stmt = (
                select(OpsHistoryRow)
                .where(*ops_history_queries.search_conditions(query))
                .order_by(*ops_history_queries.SEARCH_ORDER)
            )
            rows = session.execute(stmt).scalars().all()
            return ops_history_queries.search_matches(rows, query)
