"""A reload keeps what the record knows; only a real restart marks it unconfirmed.

A reload stops one runtime and starts the next in the same process, so nothing went unwatched.
Treating every reload as a restart put every plate, rack and head on the settle list after every package edit.
"""
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.test_system_runtime import _build_simple_system


async def _second_runtime(*, after_reload: bool) -> SystemRuntime:
    labware_service = LabwareService(InMemoryLabwareStore())
    ops_store = memory_ops_store()

    system_a, _ = await _build_simple_system(ops_store, labware_service=labware_service)
    runtime_a = await memory_runtime(system_a, labware_service=labware_service)
    await runtime_a.start()
    plate = await runtime_a.labware.register("plate_96", location="pad1", confirm=True)
    await runtime_a.labware.set_well_volumes(plate.id, {"A1": 120.0}, reason="prefilled", confirm=True)
    await runtime_a.shutdown()

    system_b, _ = await _build_simple_system(ops_store, labware_service=labware_service)
    runtime_b = await memory_runtime(system_b, labware_service=labware_service)
    await runtime_b.start(after_reload=after_reload)
    return runtime_b


async def test_a_reload_leaves_the_settle_list_empty() -> None:
    runtime = await _second_runtime(after_reload=True)
    try:
        assert await runtime.unsettled_state() == []
    finally:
        await runtime.shutdown()


async def test_a_restart_asks_for_the_plate_to_be_confirmed() -> None:
    runtime = await _second_runtime(after_reload=False)
    try:
        unsettled = await runtime.unsettled_state()
        assert [row.provenance.value for row in unsettled] == ["stale"]
    finally:
        await runtime.shutdown()
