"""Completed labware moves persist to ILabwareStore, so a restart restores
current positions instead of the positions labware was first placed at.

The gap this pins (state-ownership audit D6): the location service was
memory-only for moves, so rehydration after a crash re-placed every plate at
its registration-time position, hours stale on a long campaign.
"""

from collections.abc import AsyncGenerator

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import IMethodTemplate
import orca.orca as orca
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


async def _build_system() -> ISystem:
    """One device, one transporter, two pads, one shake workflow."""
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
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
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def _started_runtime(store: InMemoryLabwareStore) -> tuple[ISystem, SystemRuntime]:
    system = await _build_system()
    runtime = SystemRuntime(system, labware_store=store)
    await runtime.start()
    return system, runtime


class TestMovePersistsToStore:
    async def test_position_update_reaches_the_store(self) -> None:
        """A move recorded on the location service lands in the labware store."""
        store = InMemoryLabwareStore()
        plate = LabwareInstance("plate_1", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")
        system, runtime = await _started_runtime(store)

        system.labware_location_service.update(
            plate, system.system_map.get_location("pad2")
        )
        await runtime.flush_labware_location_writes()

        assert store.get_location(plate.id) == "pad2"
        await runtime.shutdown()

    async def test_restart_restores_the_moved_to_position(self) -> None:
        """The incident shape: place, move, crash, boot. The plate must come
        back where it moved to, not where it was registered."""
        store = InMemoryLabwareStore()
        plate = LabwareInstance("campaign_plate", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")

        system_a, runtime_a = await _started_runtime(store)
        system_a.labware_location_service.update(
            plate, system_a.system_map.get_location("pad2")
        )
        await runtime_a.flush_labware_location_writes()
        await runtime_a.shutdown()

        system_b, runtime_b = await _started_runtime(store)
        assert system_b.system_map.get_location("pad2").labware is not None
        assert system_b.system_map.get_location("pad1").labware is None
        await runtime_b.shutdown()

    async def test_a_plate_in_the_jaws_is_persisted_there(self) -> None:
        """Recording the slot the plate was lifted off looks safer and is not:
        after a restart the arm is still holding it, and a model that says
        otherwise sends the arm back into a slot that is now empty."""
        store = InMemoryLabwareStore()
        plate = LabwareInstance("plate_1", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")
        system, runtime = await _started_runtime(store)

        transporter = system.transporters[0]
        system.labware_location_service.update(plate, transporter.gripper_location)
        await runtime.flush_labware_location_writes()

        assert store.get_location(plate.id) == transporter.gripper_location.position_id
        await runtime.shutdown()

    async def test_a_restart_puts_the_plate_back_in_the_jaws(self) -> None:
        """The arm comes back holding what it was holding, so the next move
        places instead of reaching into the slot the plate already left."""
        store = InMemoryLabwareStore()
        plate = LabwareInstance("plate_1", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")

        system_a, runtime_a = await _started_runtime(store)
        transporter_a = system_a.transporters[0]
        system_a.labware_location_service.update(plate, transporter_a.gripper_location)
        await runtime_a.flush_labware_location_writes()
        await runtime_a.shutdown()

        system_b, runtime_b = await _started_runtime(store)
        transporter_b = system_b.transporters[0]
        assert transporter_b.labware is not None
        assert transporter_b.labware.id == plate.id
        await runtime_b.shutdown()

    async def test_unregistered_labware_is_registered_on_first_persist(self) -> None:
        """Thread-spawned labware the store never saw gets registered, so its
        position survives a restart too."""
        store = InMemoryLabwareStore()
        system, runtime = await _started_runtime(store)

        spawned = LabwareInstance("spawned_plate", "96_well")
        system.labware_location_service.update(
            spawned, system.system_map.get_location("pad1")
        )
        await runtime.flush_labware_location_writes()

        assert await store.get_by_id(spawned.id) is not None
        assert store.get_location(spawned.id) == "pad1"
        await runtime.shutdown()

    async def test_retired_labware_keeps_its_record_but_is_not_rehydrated(self) -> None:
        """Disposal must clear the stored ACTIVE position: persisting the move
        onto the end slot without retiring resurrects the consumed plate there
        on the next boot, blocking the slot for the next run (pinned e2e by the
        slow reconciliation reboot tests). A lifecycle end is not a retraction:
        the identity row and location history survive for after-the-fact reads."""
        store = InMemoryLabwareStore()
        plate = LabwareInstance("plate_1", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")
        system_a, runtime_a = await _started_runtime(store)

        system_a.labware_location_service.update(
            plate, system_a.system_map.get_location("pad2")
        )
        system_a.labware_location_service.retire(plate)
        await runtime_a.flush_labware_location_writes()
        assert await store.get_by_id(plate.id) is not None
        assert store.get_location(plate.id) is None
        assert len(await store.get_location_history(plate.id)) == 2
        await runtime_a.shutdown()

        system_b, runtime_b = await _started_runtime(store)
        assert system_b.system_map.get_location("pad2").labware is None
        assert system_b.system_map.get_location("pad1").labware is None
        await runtime_b.shutdown()

    async def test_reuse_after_retire_persists_the_new_position(self) -> None:
        """A retired labware placed again (same physical container, next run's
        reuse-bind) must persist its new position even when it matches the
        retired one; the retire cleared the active claim, not the identity."""
        store = InMemoryLabwareStore()
        plate = LabwareInstance("plate_1", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")
        system, runtime = await _started_runtime(store)

        pad1 = system.system_map.get_location("pad1")
        system.labware_location_service.update(plate, pad1)
        system.labware_location_service.retire(plate)
        system.labware_location_service.update(plate, pad1)
        await runtime.flush_labware_location_writes()

        assert store.get_location(plate.id) == "pad1"
        await runtime.shutdown()

    async def test_facade_register_with_location_writes_one_history_row(self) -> None:
        """Operator register-at-location must land exactly one location-history
        row: the chokepoint listener is the only persistence path, so a direct
        facade store write would double it."""
        store = InMemoryLabwareStore()
        system, runtime = await _started_runtime(store)

        snap = await runtime.labware.register(
            "plate_96", location="pad1", confirm=True,
        )
        await runtime.flush_labware_location_writes()

        history = await store.get_location_history(snap.id)
        assert len(history) == 1
        assert store.get_location(snap.id) == "pad1"
        await runtime.shutdown()

    async def test_facade_reset_location_persists_position_id_once(self) -> None:
        """reset_location persists the RESOLVED position id, exactly once."""
        store = InMemoryLabwareStore()
        system, runtime = await _started_runtime(store)
        snap = await runtime.labware.register(
            "plate_96", location="pad1", confirm=True,
        )
        await runtime.flush_labware_location_writes()

        await runtime.labware.reset_location(
            snap.id, "pad2", confirm=True, reason="test",
        )
        await runtime.flush_labware_location_writes()

        history = await store.get_location_history(snap.id)
        assert [row[0] for row in history] == ["pad1", "pad2"]
        assert store.get_location(snap.id) == "pad2"
        await runtime.shutdown()

    async def test_facade_edit_location_writes_one_history_row(self) -> None:
        """Operator edit_location lands one history row via the listener."""
        store = InMemoryLabwareStore()
        system, runtime = await _started_runtime(store)
        snap = await runtime.labware.register(
            "plate_96", location="pad1", confirm=True,
        )
        await runtime.flush_labware_location_writes()

        await runtime.labware.edit_location(
            snap.id, "pad2", confirm=True, reason="test",
        )
        await runtime.flush_labware_location_writes()

        history = await store.get_location_history(snap.id)
        assert [row[0] for row in history] == ["pad1", "pad2"]
        await runtime.shutdown()

    async def test_unchanged_position_writes_no_duplicate_history(self) -> None:
        """Re-recording the same position (boot re-place, idempotent updates)
        must not spam the store's location history."""
        store = InMemoryLabwareStore()
        plate = LabwareInstance("plate_1", "96_well")
        await store.register(plate)
        await store.update_location(plate.id, "pad1")
        system, runtime = await _started_runtime(store)

        pad1 = system.system_map.get_location("pad1")
        system.labware_location_service.update(plate, pad1)
        system.labware_location_service.update(plate, pad1)
        await runtime.flush_labware_location_writes()

        history = await store.get_location_history(plate.id)
        assert len(history) == 1
        await runtime.shutdown()
