"""Every SQLite activity store passes the checks in `orca.store_contract`."""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from orca.modes.device_modes import IDeviceModes
from orca.resource_models.store_interfaces import ILabwareStore
from orca.runtime.audit_store import IAuditStore
from orca.runtime.db import create_all_tables, create_memory_engine, make_session_factory
from orca.runtime.event_store import IEventStore
from orca.runtime.execution_record_store import IExecutionRecordStore
from orca.runtime.incident_store import IIncidentStore
from orca.runtime.sql_labware_store import SqlLabwareStore
from orca.runtime.sqlite_audit_store import SqliteAuditStore
from orca.runtime.sqlite_device_mode_store import SqliteDeviceModeStore
from orca.runtime.sqlite_event_store import SqliteEventStore
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.sqlite_ops_history_store import SqliteOpsHistoryStore
from orca.state.ops_store import IOpsHistoryStore
from orca.store_contract import (
    AUDIT_STORE_CHECKS,
    DEVICE_MODE_STORE_CHECKS,
    EVENT_STORE_CHECKS,
    EXECUTION_RECORD_STORE_CHECKS,
    INCIDENT_STORE_CHECKS,
    LABWARE_STORE_CHECKS,
    OPS_HISTORY_STORE_CHECKS,
)
from tests.ephemeral_ops_store import ops_history_engine


def _name(check: Callable[..., Awaitable[None]]) -> str:
    return check.__name__


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_memory_engine()
    await create_all_tables(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
def ops_history_store(tmp_path: Path) -> Iterator[SqliteOpsHistoryStore]:
    blocking = ops_history_engine(tmp_path / "ops.db")
    yield SqliteOpsHistoryStore(blocking)
    blocking.dispose()


@pytest.mark.parametrize("check", LABWARE_STORE_CHECKS, ids=_name)
async def test_the_labware_store(check: Callable[[ILabwareStore], Awaitable[None]], engine: AsyncEngine) -> None:
    await check(SqlLabwareStore(make_session_factory(engine)))


@pytest.mark.parametrize("check", INCIDENT_STORE_CHECKS, ids=_name)
async def test_the_incident_store(check: Callable[[IIncidentStore], Awaitable[None]], engine: AsyncEngine) -> None:
    await check(SqliteIncidentStore(engine))


@pytest.mark.parametrize("check", EXECUTION_RECORD_STORE_CHECKS, ids=_name)
async def test_the_execution_record_store(
    check: Callable[[IExecutionRecordStore], Awaitable[None]], engine: AsyncEngine,
) -> None:
    await check(SqliteExecutionRecordStore(engine))


@pytest.mark.parametrize("check", OPS_HISTORY_STORE_CHECKS, ids=_name)
async def test_the_ops_history_store(
    check: Callable[[IOpsHistoryStore], Awaitable[None]], ops_history_store: SqliteOpsHistoryStore,
) -> None:
    await check(ops_history_store)


@pytest.mark.parametrize("check", DEVICE_MODE_STORE_CHECKS, ids=_name)
async def test_the_device_mode_store(check: Callable[[IDeviceModes], Awaitable[None]], engine: AsyncEngine) -> None:
    await check(SqliteDeviceModeStore(engine))


@pytest.mark.parametrize("check", AUDIT_STORE_CHECKS, ids=_name)
async def test_the_audit_store(check: Callable[[IAuditStore], Awaitable[None]], engine: AsyncEngine) -> None:
    await check(SqliteAuditStore(engine))


@pytest.mark.parametrize("check", EVENT_STORE_CHECKS, ids=_name)
async def test_the_event_store(check: Callable[[IEventStore], Awaitable[None]], engine: AsyncEngine) -> None:
    await check(SqliteEventStore(engine))
