"""Labware-location rehydration on SystemRuntime.start().

After a crash, ILabwareStore knows where each plate was at last shutdown.
SystemRuntime.start() must re-place those plates on the System graph so the
runtime resumes from the same physical state.
"""

from orca.config import OrcaConfig
from collections.abc import AsyncGenerator

import pytest

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.sdk.events import EventBus
from orca.sdk.labware import PlateTemplate
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
import orca.orca as orca
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from orca.resource_models.labware_service_interface import ILabwareService
from tests.memory_activity import memory_labware_service


async def _build_simple_system(*, labware_service: ILabwareService | None = None) -> ISystem:
    """Minimal system: one device, one transporter, one location pad1."""
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)

    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: object) -> AsyncGenerator[object, None]:
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: object) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method

    workflow = WorkflowTemplate("simple_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=labware_service or memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system()


class TestStoreListActiveLocations:
    """Exercise the new list_active_locations() method on the InMemory impl."""

    async def test_in_memory_returns_empty_when_no_locations_set(self) -> None:
        store = InMemoryLabwareStore()
        assert await store.list_active_locations() == []

    async def test_in_memory_returns_pairs_after_update_location(self) -> None:
        store = InMemoryLabwareStore()
        instance = LabwareInstance("plate_1", "96_well")
        await store.register(instance)
        await store.update_location(instance.id, "pad1")
        pairs = await store.list_active_locations()
        assert pairs == [(instance.id, "pad1")]


class TestRehydrateOnStart:
    """SystemRuntime.start() re-places persisted labware on the System graph."""

    async def test_persisted_labware_appears_on_location_after_start(self) -> None:
        store = InMemoryLabwareStore()
        system = await _build_simple_system(labware_service=LabwareService(store))

        plate = LabwareInstance("plate_to_restore", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")

        runtime = await memory_runtime(system)
        await runtime.start()

        location = system.system_map.get_location("pad1")
        # rehydrate calls _labware_service.get_by_id which returns the registered instance
        assert location.labware is not None
        assert location.labware.id == plate.id

        await runtime.shutdown()

    async def test_labware_restored_before_its_workflow_loads_becomes_its_real_kind(self) -> None:
        store = InMemoryLabwareStore()
        system = await _build_simple_system(labware_service=LabwareService(store))
        persisted = LabwareInstance("late_plate", "96_well")
        await store.register(persisted)
        await store.update_location(persisted.id, "pad1")
        runtime = await memory_runtime(system)
        await runtime.start()
        assert not system.system_map.get_location("pad1").labware.has_plr_backing

        late_plate = PlateTemplate("late_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
        pad = system.system_map.get_location("pad1")

        @orca.method
        async def nothing(ctx: MethodContext) -> AsyncGenerator[None, None]:
            return
            yield

        @orca.thread(labware=late_plate, start=pad, end=pad)
        async def late_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
            yield nothing

        workflow = WorkflowTemplate("late_workflow")
        workflow.add_thread(late_thread, is_start=True)
        await runtime.registry.add_workflow_template(workflow, confirm=True)

        assert system.system_map.get_location("pad1").labware.has_plr_backing
        await runtime.shutdown()

    async def test_no_persisted_labware_leaves_locations_empty(self) -> None:
        system = await _build_simple_system()
        runtime = await memory_runtime(system)
        await runtime.start()

        location = system.system_map.get_location("pad1")
        assert location.labware is None

        await runtime.shutdown()

    async def test_unknown_location_logged_and_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        """If a persisted labware references a location no longer in the System graph,
        log a warning and continue rather than crashing the runtime."""
        store = InMemoryLabwareStore()
        system = await _build_simple_system(labware_service=LabwareService(store))

        orphan = LabwareInstance("orphan_plate", "96_well")
        await store.register(orphan)
        await store.update_location(orphan.id, "decommissioned_pad")

        runtime = await memory_runtime(system)
        with caplog.at_level("WARNING"):
            await runtime.start()

        assert any("decommissioned_pad" in rec.message for rec in caplog.records)
        await runtime.shutdown()

    async def test_unknown_labware_id_logged_and_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        """If list_active_locations() returns an id get_by_id can't resolve, skip with a warning."""

        class _BrokenStore(InMemoryLabwareStore):
            async def list_active_locations(self) -> list[tuple[str, str]]:
                return [("nonexistent_id", "pad1")]

        system = await _build_simple_system(labware_service=LabwareService(_BrokenStore()))
        runtime = await memory_runtime(system)
        with caplog.at_level("WARNING"):
            await runtime.start()

        assert any("nonexistent_id" in rec.message for rec in caplog.records)
        location = system.system_map.get_location("pad1")
        assert location.labware is None
        await runtime.shutdown()


class TestClearAfterRehydrate:
    """PIN: after a restart rehydrates persisted labware down to the leaves,
    clear_all_labware cascades to those rehydrated leaves in ONE shot.

    Guards the register-before-place ordering in
    ``SystemRuntime._rehydrate_labware_locations``: rehydrate registers the SAME
    instance object it then places on the leaf, so clear_all (which iterates
    ``system.labwares``) finds the rehydrated plate and ``location.labware is
    instance`` matches, disposing the leaf. Without that ordering the leaf would
    survive the clear and the plate would be stuck until a second restart. Pins
    the cascade so resuming durable persistence cannot silently regress it.
    """

    async def test_clear_all_cascades_to_rehydrated_leaf(self) -> None:
        store = InMemoryLabwareStore()
        system = await _build_simple_system(labware_service=LabwareService(store))
        plate = LabwareInstance("plate_to_restore", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")

        runtime = await memory_runtime(system)
        await runtime.start()

        pad1 = system.system_map.get_location("pad1")
        # Precondition: rehydrate placed the persisted plate on the leaf.
        assert pad1.labware is not None and pad1.labware.id == plate.id

        await runtime.labware.clear_all_labware(force=True, confirm=True)

        # One clear; every mirror layer agrees the plate is gone.
        assert pad1.labware is None, "leaf survived clear_all after rehydrate"
        assert not [lw for lw in system.labwares if lw.id == plate.id], (
            "engine registry still holds the rehydrated plate after clear_all"
        )
        assert await store.list_active_locations() == [], (
            "labware store still holds a position after clear_all"
        )
        assert system.labware_location_service.get_all() == {}, (
            "location ledger still holds the plate after clear_all"
        )

        await runtime.shutdown()
