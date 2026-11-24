"""Each device's sim or live setting in the framework's SQLite activity database."""

from sqlalchemy.ext.asyncio import AsyncEngine

from orca.runtime.db import create_tables, device_mode_queries, make_session_factory
from orca.runtime.db.base import Base
from orca.runtime.db.models import DeviceModeRow
from orca.modes.device_modes import DeviceMode


class SqliteDeviceModeStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sf = make_session_factory(engine)
        self._schema_ready = False

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(create_tables, [Base.metadata.tables[DeviceModeRow.__tablename__]])
        self._schema_ready = True

    async def mode_of(self, device_name: str) -> DeviceMode:
        await self._ensure_schema()
        async with self._sf() as session:
            return await device_mode_queries.mode_of(session, device_name)

    async def set(self, device_name: str, mode: DeviceMode) -> None:
        await self._ensure_schema()
        async with self._sf() as session:
            await device_mode_queries.set_mode(session, device_name, mode)
