"""The labware store over SQLite. Its reads and writes are in ``orca.runtime.db.labware_queries``."""

from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from orca.resource_models.labware import LabwareInstance
from orca.runtime.db import labware_queries


AsyncSessionFactory = Callable[[], AsyncSession]


class SqlLabwareStore:
    """ILabwareStore over a SQLAlchemy async session factory."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def register(
        self, instance: LabwareInstance, execution_id: str | None = None
    ) -> None:
        async with self._session_factory() as session:
            await labware_queries.register(session, instance, execution_id)

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        async with self._session_factory() as session:
            return await labware_queries.get_by_id(session, labware_id)

    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None:
        async with self._session_factory() as session:
            return await labware_queries.get_by_barcode(session, barcode)

    async def get_by_position(self, position_id: str) -> LabwareInstance | None:
        async with self._session_factory() as session:
            return await labware_queries.get_by_position(session, position_id)

    async def list_active_locations(self) -> list[tuple[str, str]]:
        async with self._session_factory() as session:
            return await labware_queries.list_active_locations(session)

    async def list_departed_locations(self) -> list[tuple[str, str]]:
        async with self._session_factory() as session:
            return await labware_queries.list_departed_locations(session)

    async def update_location(self, labware_id: str, position_id: str) -> None:
        async with self._session_factory() as session:
            await labware_queries.update_location(session, labware_id, position_id)

    async def clear_location(self, labware_id: str) -> None:
        async with self._session_factory() as session:
            await labware_queries.clear_location(session, labware_id)

    async def get_location_history(
        self, labware_id: str,
    ) -> list[tuple[str, str | None, float]]:
        async with self._session_factory() as session:
            return await labware_queries.get_location_history(session, labware_id)
