"""OpsHistoryFacade: read-only UI surface over the IOpsHistoryStore archive.

Reads only -- there are no operator writes against ops history (records are
emitted by the action-execution pipeline through TrackingContext). REST and
MCP both program against ``runtime.ops_history.<method>``.
"""

from typing import List, Tuple

from orca.state.records import TrackingRecord
from orca.state.ops_store import (
    IOpsHistoryStore,
    OpsHistorySearchQuery,
)
from orca.runtime.runtime_interface import IOpsHistoryFacade


class OpsHistoryFacade(IOpsHistoryFacade):
    """Concrete OpsHistoryFacade implementation."""

    def __init__(self, store: IOpsHistoryStore) -> None:
        self._store = store

    async def list(self, execution_id: str) -> List[TrackingRecord]:
        return await self._store.list(execution_id)

    async def search(
        self, query: OpsHistorySearchQuery,
    ) -> List[Tuple[str, TrackingRecord]]:
        return await self._store.search(query)
