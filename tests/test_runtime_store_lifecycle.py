"""Activity stores are passed to SystemRuntime and closed by whoever opened them.

A store's aclose disposes its in-memory engine, so its aiosqlite worker thread
does not outlive the event loop and error at teardown
(``RuntimeError: Event loop is closed``).
"""

from orca.config import OrcaConfig
import asyncio
import inspect
import threading
import time
from unittest.mock import MagicMock
from typing import get_type_hints
from collections.abc import Sequence

import pytest

from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.resources import IResource
from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record_service import ExecutionRecordService, IExecutionRecordService
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.state.ops_store import SYSTEM_ID
from orca.state.records import TrackingRecord, TrackingSource
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.unsaved_records import UnsavedRecords
from tests.memory_activity import memory_labware_service


def _worker_idents() -> set[int]:
    return {
        t.ident
        for t in threading.enumerate()
        if "_connection_worker" in (t.name or "") and t.ident is not None
    }


async def _settle_gone(idents: set[int]) -> bool:
    for _ in range(100):
        await asyncio.sleep(0.02)
        if idents.isdisjoint(_worker_idents()):
            return True
    return False


async def _build_minimal_system(
    extra_resources: Sequence[IResource] = (),
) -> ISystem:
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    for resource in extra_resources:
        registry.add_resource(resource)
    registry.add_resource_pool(ResourcePool("shaker1", [device]))

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    builder = SdkToSystemBuilder(
        name="t", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[WorkflowTemplate("wf")], event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def test_sqlite_store_aclose_reclaims_worker_thread() -> None:
    """Dropping the StaticPool skip: a store's aclose disposes its in-memory
    engine so the aiosqlite worker thread does not outlive the loop."""
    pre = _worker_idents()
    store = SqliteIncidentStore(create_memory_engine())
    await store.create_schema()
    mine = _worker_idents() - pre
    assert mine, "expected a new aiosqlite worker thread while the engine is live"

    await store.aclose()

    assert await _settle_gone(mine), (
        "store.aclose did not reclaim its aiosqlite worker thread"
    )


async def test_operator_contract_exposes_the_injected_execution_record_service() -> None:
    """A surface typed on ``ISystemRuntime`` reads terminal executions through
    the SAME service the deployment injected.

    The get/list/detail execution Operations take their terminal-record
    fallbacks from here. If the contract hid this Service, a surface holding
    only the protocol would report a completed execution as missing the moment
    the runtime evicts it -- and a hosted deployment sourcing the Service some
    other way could read a DIFFERENT store than the runtime writes to.
    """
    # hasattr alone would pass on a member declared with the wrong type, and
    # this repo's CI runs no type checker, so pin the annotation too.
    prop = inspect.getattr_static(ISystemRuntime, "execution_records")
    declared = get_type_hints(prop.fget).get("return")
    assert declared is IExecutionRecordService, (
        "the operator contract must declare execution_records as the "
        f"IExecutionRecordService; got {declared!r}. A surface typed on the "
        "protocol cannot wire the terminal fallback without it."
    )

    injected = ExecutionRecordService(
        SqliteExecutionRecordStore(create_memory_engine()), UnsavedRecords(),
    )
    system = await _build_minimal_system()
    runtime = await memory_runtime(system, execution_record_service=injected)

    assert runtime.execution_records is injected

    await runtime.shutdown(confirm=True)
    await injected.aclose()


_EVERY_SERVICE = (
    "incident_service", "execution_record_service",
    "access_config_service", "move_defaults_service", "grip_profile_service",
    "profile_store", "labware_catalog_store", "gateway_registry", "connection_source",
    "device_gateway", "device_modes",
)


@pytest.mark.parametrize("missing", _EVERY_SERVICE)
def test_a_runtime_is_never_built_without_each_service(missing: str) -> None:
    """No store or service is made up inside the runtime; the server that starts it passes each one."""
    services = {name: MagicMock() for name in _EVERY_SERVICE}
    del services[missing]

    with pytest.raises(TypeError, match=missing):
        SystemRuntime(MagicMock(), **services)


async def test_the_runtime_reads_operations_from_the_store_the_system_writes() -> None:
    system = await _build_minimal_system()
    runtime = await memory_runtime(system)

    await system.ops_history.append_record(TrackingRecord(
        execution_id=SYSTEM_ID, action_id="a1", thread_id="t1", method_id="m1",
        source=TrackingSource.OBSERVED, timestamp=time.time(), operations=[],
    ))

    assert [r.action_id for r in await runtime.ops_history.list(SYSTEM_ID)] == ["a1"]

    await runtime.shutdown(confirm=True)
