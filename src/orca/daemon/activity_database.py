"""What the lab does, in one SQLite file: the daemon's own, and one per check run."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from orca.runtime.event_saves import EventSaves
from orca.runtime.audit_store import IAuditStore
from orca.runtime.db import create_blocking_sqlite_engine, create_sqlite_engine, create_tables
from orca.runtime.db.base import Base
from orca.modes.device_modes import IDeviceModes
from orca.runtime.event_store import IEventStore
from orca.runtime.execution_record_service import IExecutionRecordService
from orca.runtime.incident_service import IIncidentService
from orca.resource_models.labware_service_interface import ILabwareService
from orca.runtime.sql_labware_store import SqlLabwareStore
from orca.runtime.sqlite_audit_store import SqliteAuditStore
from orca.runtime.sqlite_event_store import SqliteEventStore
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.sqlite_ops_history_store import SqliteOpsHistoryStore
from orca.runtime.sqlite_device_mode_store import SqliteDeviceModeStore
from orca.runtime.startup import ActivityStores, build_activity
from orca.runtime.unsaved_record import IUnsavedRecordReporter
from orca.state.ops_store import IOpsHistoryStore


_ACTIVITY_TABLES = (
    "labware_instances", "labware_events", "ops_history", "ops_history_labware", "ops_history_device",
    "runtime_events",
)


class ActivityDatabase:
    """What the lab does, in one SQLite file in the package folder."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The package folder is often a git repo; activity never goes in it.
        (path.parent / ".gitignore").write_text("*\n", encoding="utf-8")
        engine = create_sqlite_engine(path)
        self._engine: AsyncEngine = engine
        self._blocking_engine = create_blocking_sqlite_engine(path)
        self._ops_history = SqliteOpsHistoryStore(self._blocking_engine)
        self._activity = build_activity(ActivityStores(
            labware=SqlLabwareStore(async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)),
            incidents=SqliteIncidentStore(engine),
            execution_records=SqliteExecutionRecordStore(engine),
            ops_history=self._ops_history,
            device_modes=SqliteDeviceModeStore(engine),
        ))
        self.audit: IAuditStore = SqliteAuditStore(engine)
        self.events: IEventStore = SqliteEventStore(engine)
        self._opened = False

    @property
    def labware(self) -> ILabwareService:
        return self._activity.labware

    @property
    def incidents(self) -> IIncidentService:
        return self._activity.incidents

    @property
    def execution_records(self) -> IExecutionRecordService:
        return self._activity.execution_records

    @property
    def ops_history(self) -> IOpsHistoryStore:
        return self._activity.ops_history

    @property
    def device_modes(self) -> IDeviceModes:
        return self._activity.device_modes

    @property
    def unsaved_records(self) -> IUnsavedRecordReporter:
        return self._activity.unsaved_records

    @property
    def event_saves(self) -> EventSaves:
        return self._activity.event_saves

    async def open(self) -> None:
        """Create the schema and start the writers. Only the first call does anything."""
        if self._opened:
            return
        self._opened = True
        async with self._engine.begin() as conn:
            await conn.run_sync(create_tables, [Base.metadata.tables[name] for name in _ACTIVITY_TABLES])
        # The framework builds its tables without migrations, so it fills the device index itself.
        await self._ops_history.index_old_records_by_device()
        await self._activity.create_schema()
        self._activity.start()

    async def aclose(self) -> None:
        await self._activity.aclose()
        # The services above close their own handles; the stores without one share this engine.
        await self._engine.dispose()
        self._blocking_engine.dispose()
