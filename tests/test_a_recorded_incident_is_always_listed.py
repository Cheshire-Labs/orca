"""An incident that was recorded is listed, even when it is saved while another read is still open.

The in-memory database has one connection. A read that closed its session there rolled back a save that
had written its row and not yet committed, so the incident was neither stored nor kept: it was gone.
"""

import asyncio

import pytest
from sqlalchemy import select

from orca.runtime.db import create_memory_engine
from orca.runtime.db.incident_mapping import incident_to_row, row_to_incident
from orca.runtime.db.models import IncidentRow
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    IncidentCategory, IncidentSeverity, OtherIncidentDetail, RecoveryAction, SystemIncident,
)
from orca.runtime.sqlite_incident_store import SqliteIncidentStore

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]

_A_CHANCE_TO_RUN_SECONDS = 1.0


async def _give_it_a_chance(reached: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(reached.wait(), timeout=_A_CHANCE_TO_RUN_SECONDS)
    except asyncio.TimeoutError:
        pass


class _ReadsAndSavesOverlapIfTheyCan(SqliteIncidentStore):
    """A read keeps its session open until a save has written its row, if a save is able to run beside it."""

    def __init__(self) -> None:
        super().__init__(create_memory_engine())
        self.reading = asyncio.Event()
        self.row_written = asyncio.Event()
        self.read_closed = asyncio.Event()

    async def fetch(
        self, *, unacknowledged_only: bool = False, category: IncidentCategory | None = None,
        execution_id: str | None = None, since: float | None = None,
    ) -> list[SystemIncident]:
        await self.create_schema()
        async with self._sf() as session:
            rows = (await session.execute(select(IncidentRow).order_by(IncidentRow.timestamp))).scalars().all()
            if not self.reading.is_set():
                self.reading.set()
                await _give_it_a_chance(self.row_written)
        self.read_closed.set()
        return [row_to_incident(row) for row in rows]

    async def insert(self, incident: SystemIncident) -> None:
        await self.create_schema()
        async with self._sf() as session:
            session.add(incident_to_row(incident))
            await session.flush()
            self.row_written.set()
            if self.reading.is_set():
                await _give_it_a_chance(self.read_closed)
            await session.commit()


def _record(service: IncidentService) -> SystemIncident:
    return service.record(
        category=IncidentCategory.OTHER, severity=IncidentSeverity.WARNING, message="recorded during a read",
        detail=OtherIncidentDetail(message_extra=""), recovery_action=RecoveryAction.NONE,
    )


async def test_an_incident_recorded_while_a_list_is_reading_is_listed_afterwards() -> None:
    store = _ReadsAndSavesOverlapIfTheyCan()
    service = IncidentService(store)
    await service.ensure_schema()
    service.start_drain_task()
    try:
        first_list = asyncio.create_task(service.list())
        await asyncio.wait_for(store.reading.wait(), timeout=5.0)

        recorded = _record(service)
        await first_list

        assert [incident.id for incident in await service.list()] == [recorded.id]
    finally:
        await service.aclose()
