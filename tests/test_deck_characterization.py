"""Characterization tests for the action pipeline's multi-labware behavior.

These tests capture the CURRENT behavior of:
1. Multi-labware convergence at a single device Location
2. The stage -> loaded_labware pipeline (StageLoadingEquipmentLabwareManager)
3. The missing-input view (peek_missing_input_labware) and the all_labware_is_present event
4. The hold-over logic in the thread loop (get_potential_locations)
5. How location.resource is used throughout the action lifecycle

Purpose: establish empirical baselines BEFORE any deck integration changes.
If any of these tests break during refactoring, the change has altered
existing behavior and must be understood before proceeding.
"""

from collections.abc import AsyncGenerator
from typing import List
from unittest.mock import AsyncMock

import pytest

import orca.orca as orca
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.executors import StandaloneMethodExecutor
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.dynamic_resource_action import DynamicResourceActionResolver
from tests.mock import EXTERNAL_MOVER, UniversalMockDevice
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from tests.test_helpers import (
    wire_system_map,
    create_test_plate_template,
    create_test_transporter,
)


async def _noop_action_body(ctx: ActionContext) -> None:
    del ctx


async def _build_multi_labware_system(
    device_names: list[str],
    pad_names: list[str],
    site_names: list[str] | None = None,
) -> tuple[list[UniversalMockDevice], list[ResourcePool], ResourceRegistry, SystemMap]:
    """Build a test system with devices and pads connected by one transporter."""
    all_position_ids = device_names + pad_names
    transporter = create_test_transporter("robot1", all_position_ids)

    registry = ResourceRegistry()
    registry.add_resource(transporter)

    devices: list[UniversalMockDevice] = []
    pools: list[ResourcePool] = []
    for name in device_names:
        device = UniversalMockDevice(name, site_names=site_names)
        registry.add_resource(device)
        pool = ResourcePool(name, [device])
        registry.add_resource_pool(pool)
        pools.append(pool)
        devices.append(device)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={device.name: device for device in devices},
        pads=pad_names,
    )

    return devices, pools, registry, system_map


class TestLocationResourceIsLabwareStagingBridge:
    """Verify that location.resource is a LabwareStagingBridge wrapping the Device."""

    async def test_location_resource_is_labware_staging_bridge_after_assignment(self) -> None:
        """Once a device is assigned, location.resource returns the LabwareStagingBridge."""
        devices, _pools, _registry, system_map = await _build_multi_labware_system(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        site = system_map.get_location("shaker1/slot")

        assert isinstance(site.resource, LabwareStagingBridge)
        assert site.resource.name == "shaker1/slot"
        # The bare device name is the off-graph reservation mutex.
        assert system_map.get_location("shaker1").position_id == "shaker1"

    async def test_location_resource_is_platepad_for_unassigned_locations(self) -> None:
        """Locations without explicit resource assignment get a default PlatePad."""
        _devices, _pools, _registry, system_map = await _build_multi_labware_system(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        pad_location = system_map.get_location("pad1")

        assert isinstance(pad_location.resource, PlatePad)


class TestEquipmentMapLookup:
    """Verify how SystemMap._equipment_map is populated and queried."""

    async def test_get_resource_location_finds_assigned_device(self) -> None:
        """get_resource_location returns the Location where a device was assigned."""
        devices, _pools, _registry, system_map = await _build_multi_labware_system(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        location = system_map.get_resource_location("shaker1")

        # The equipment map resolves a device to its reservation MUTEX
        # Its labware sits at the flat site node.
        assert location.name == "shaker1"
        assert isinstance(
            system_map.get_location("shaker1/slot").resource, LabwareStagingBridge
        )

    async def test_get_resource_location_raises_for_unknown_device(self) -> None:
        """get_resource_location raises ValueError for unregistered device names."""
        _devices, _pools, _registry, system_map = await _build_multi_labware_system(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        with pytest.raises(ValueError):
            system_map.get_resource_location("nonexistent_device")


class TestGetPotentialLocations:
    """Verify DynamicResourceActionResolver.get_potential_locations behavior."""

    async def test_single_device_pool_returns_one_location(self) -> None:
        """A pool with one device returns exactly one potential location."""
        devices, pools, _registry, system_map = await _build_multi_labware_system(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        resolver = DynamicResourceActionResolver(
            reservation_coordinator=AsyncMock(),
            system_map=system_map,
        )

        # Create UnresolvedLocationAction directly (how it's built internally)
        plate = create_test_plate_template("plate")
        action_template = orca.action(device=pools[0], inputs=[plate])(
            _noop_action_body
        )
        from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
        unresolved = UnresolvedLocationAction(
            resource=action_template.resource_pool,
            location_action=action_template.get_location_action(),
            expected_input_templates=action_template.inputs,
            expected_output_templates=action_template.outputs,
        )

        locations = resolver.get_potential_locations(unresolved)

        assert len(locations) == 1
        loc = next(iter(locations))
        # The single candidate is the device's reservation mutex; the bridge
        # holder lives at the flat site node.
        assert loc.name == "shaker1"
        assert isinstance(
            system_map.get_location("shaker1/slot").resource, LabwareStagingBridge
        )

    async def test_multi_device_pool_returns_all_locations(self) -> None:
        """A pool with multiple devices returns all their locations."""
        devices, _pools, registry, system_map = await _build_multi_labware_system(
            device_names=["shaker1", "shaker2"],
            pad_names=["pad1"],
        )
        combined_pool = ResourcePool("shaker_pool", devices)
        registry.add_resource_pool(combined_pool)

        resolver = DynamicResourceActionResolver(
            reservation_coordinator=AsyncMock(),
            system_map=system_map,
        )

        plate = create_test_plate_template("plate")
        action_template = orca.action(device=combined_pool, inputs=[plate])(
            _noop_action_body
        )
        from orca.workflow_models.actions.dynamic_resource_action import UnresolvedLocationAction
        unresolved = UnresolvedLocationAction(
            resource=action_template.resource_pool,
            location_action=action_template.get_location_action(),
            expected_input_templates=action_template.inputs,
            expected_output_templates=action_template.outputs,
        )

        locations = resolver.get_potential_locations(unresolved)

        assert len(locations) == 2
        names = {loc.name for loc in locations}
        assert names == {"shaker1", "shaker2"}


class TestStageLoadingPipeline:
    """Verify the stage -> loaded_labware lifecycle on LabwareStagingBridge."""

    async def test_notify_placed_moves_to_loaded(self) -> None:
        """notify_placed stages labware then moves it to loaded_labware."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()

        assert nest.labware is None
        assert len(nest.loaded_labware) == 0

        await (nest.notify_placed(labware, EXTERNAL_MOVER))

        assert nest.accessible_labware is None  # staged cleared into loaded
        assert nest.labware is not None  # the loaded plate still occupies
        assert labware in nest.loaded_labware

    async def test_second_place_while_first_loaded_is_refused(self) -> None:
        """A single-slot device holds ONE plate: 'loaded' means clamped in
        place on the same physical position, so a second place while the
        first is loaded would stack plates and must be refused."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        lw1 = await create_test_plate_template("plate_a").create_instance()
        lw2 = await create_test_plate_template("plate_b").create_instance()

        await nest.notify_placed(lw1, EXTERNAL_MOVER)

        assert nest.labware is lw1, "a loaded plate keeps the site occupied"
        with pytest.raises(SlotOccupiedError, match="already holds"):
            await nest.prepare_for_place(lw2, EXTERNAL_MOVER)

    async def test_prepare_for_pick_unloads_to_stage(self) -> None:
        """prepare_for_pick moves labware from loaded back to stage."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()

        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        assert labware in nest.loaded_labware

        await (nest.prepare_for_pick(labware, EXTERNAL_MOVER))

        assert nest.labware == labware  # now staged
        assert labware not in nest.loaded_labware

    async def test_notify_picked_clears_stage(self) -> None:
        """notify_picked clears the stage after transporter picks."""
        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        labware = await create_test_plate_template("plate").create_instance()

        await (nest.notify_placed(labware, EXTERNAL_MOVER))
        await (nest.prepare_for_pick(labware, EXTERNAL_MOVER))
        await (nest.notify_picked(labware, EXTERNAL_MOVER))

        assert nest.labware is None
        assert len(nest.loaded_labware) == 0


class TestMultiLabwareConvergence:
    """Verify that multiple labware items converge at the SAME device Location.

    This is the critical characterization: in the current model, ALL threads
    sharing an action route to ONE action.location. The device's
    StageLoadingEquipmentLabwareManager handles sequential arrival.
    """

    @pytest.mark.asyncio
    async def test_two_labware_converge_at_same_device_location(self) -> None:
        """Two labware converge on ONE shared action that runs exactly once."""
        devices, pools, registry, system_map = await _build_multi_labware_system(
            device_names=["lh1"],
            pad_names=["pad1", "pad2"],
            site_names=["site-1", "site-2"],
        )
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")
        pool = pools[0]

        executed: list[str] = []

        @orca.action(device=pool, inputs=[plate_a, plate_b])
        async def shared_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)
            executed.append("ran")

        @orca.method
        async def shared_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield shared_action

        builder = SdkToSystemBuilder(
            name="convergence_test",
            description="",
            labwares=[plate_a, plate_b],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=shared_method,
            labware_start_mapping={plate_a: "pad1", plate_b: "pad2"},
            labware_end_mapping={plate_a: "pad1", plate_b: "pad2"},
            system=system,
        )
        await executor.start()

        assert executed == ["ran"], "two converging labware share one action execution"

    @pytest.mark.asyncio
    async def test_three_labware_converge_at_same_device_location(self) -> None:
        """Three labware converge on ONE shared action that runs exactly once."""
        devices, pools, registry, system_map = await _build_multi_labware_system(
            device_names=["lh1"],
            pad_names=["pad1", "pad2", "pad3"],
            site_names=["site-1", "site-2", "site-3"],
        )
        plate_a = create_test_plate_template("plate_a")
        plate_b = create_test_plate_template("plate_b")
        plate_c = create_test_plate_template("plate_c")
        pool = pools[0]

        executed: list[str] = []

        @orca.action(device=pool, inputs=[plate_a, plate_b, plate_c])
        async def three_way_action(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)
            executed.append("ran")

        @orca.method
        async def shared_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield three_way_action

        builder = SdkToSystemBuilder(
            name="three_way_test",
            description="",
            labwares=[plate_a, plate_b, plate_c],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=shared_method,
            labware_start_mapping={
                plate_a: "pad1", plate_b: "pad2", plate_c: "pad3",
            },
            labware_end_mapping={
                plate_a: "pad1", plate_b: "pad2", plate_c: "pad3",
            },
            system=system,
        )
        await executor.start()

        assert executed == ["ran"], "three converging labware share one action execution"


class TestAllLabwarePresentEvent:
    """Verify the observer notification and all_labware_is_present event timing."""

    async def test_location_observer_fires_on_placement(self) -> None:
        """Placing labware at a Location notifies registered observers."""
        from orca.resource_models.location import ILabwareLocationObserver

        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        location = Location("dev1", nest)

        events: list[tuple[str, str]] = []

        class TestObserver(ILabwareLocationObserver):
            async def notify_labware_location_change(
                self, event: str, location: Location, labware: LabwareInstance
            ) -> None:
                events.append((event, labware.name))

        observer = TestObserver()
        location.add_observer(observer)

        labware = await create_test_plate_template("plate").create_instance()
        await (location.notify_placed(labware, EXTERNAL_MOVER))

        assert len(events) == 1
        assert events[0][0] == "placed"

    async def test_observer_fires_before_loaded_labware_populated(self) -> None:
        """Verify whether observer fires before or after labware enters loaded_labware.

        This is important for understanding when the missing-input view
        would see the labware as present.
        """
        from orca.resource_models.location import ILabwareLocationObserver

        device = UniversalMockDevice("dev1")
        nest = LabwareStagingBridge("dev1", device)
        location = Location("dev1", nest)

        labware_was_loaded_when_observer_fired: list[bool] = []

        class TimingObserver(ILabwareLocationObserver):
            async def notify_labware_location_change(
                self, event: str, location: Location, labware: LabwareInstance
            ) -> None:
                if event == "placed":
                    labware_was_loaded_when_observer_fired.append(
                        labware in nest.loaded_labware
                    )

        observer = TimingObserver()
        location.add_observer(observer)

        labware = await create_test_plate_template("plate").create_instance()
        await (location.notify_placed(labware, EXTERNAL_MOVER))

        assert len(labware_was_loaded_when_observer_fired) == 1
        # Location.notify_placed calls resource.notify_placed (which loads labware)
        # THEN fires observers. So labware IS loaded when observer fires.
        assert labware_was_loaded_when_observer_fired[0] is True


class TestHoldOverLogic:
    """Verify the reservation hold-over behavior between consecutive actions.

    When two actions at the same device are in sequence, the thread holds
    the reservation from the first action to avoid another thread stealing
    the device between actions.
    """

    @pytest.mark.asyncio
    async def test_consecutive_actions_at_same_device_complete(self) -> None:
        """Two consecutive actions at the same device don't deadlock.

        Verifies the hold-over behavior: the thread keeps its reservation
        between actions at the same device to prevent another thread from
        stealing the device.
        """
        devices, pools, registry, system_map = await _build_multi_labware_system(
            device_names=["shaker1"],
            pad_names=["pad1"],
        )
        pool = pools[0]
        plate = create_test_plate_template("plate")

        @orca.action(device=pool, inputs=[plate])
        async def action_1(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.action(device=pool, inputs=[plate])
        async def action_2(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        @orca.method
        async def two_actions(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield action_1
            yield action_2

        builder = SdkToSystemBuilder(
            name="holdover_test",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=two_actions,
            labware_start_mapping={plate: "pad1"},
            labware_end_mapping={plate: "pad1"},
            system=system,
        )
        await executor.start()


class TestMultiHopRouting:
    """Verify that multi-hop routing (A -> B -> C) works correctly.

    This is critical for deck integration: labware routes from a pad
    to handoff (external arm) then from handoff to deck slot (iSWAP).
    Multi-hop means the SystemMap finds a path through intermediate
    locations when no single transporter can reach source and target.
    """

    @pytest.mark.asyncio
    async def test_labware_traverses_intermediate_location_to_reach_device(self) -> None:
        """Labware at pad1 routes through pad2 (intermediate) to reach device.

        Topology: pad1 -- pad2 -- device1
        (arm1 reaches pad1+pad2, arm2 reaches pad2+device1)

        The thread must make two hops: pad1->pad2, pad2->device1.
        """
        device = UniversalMockDevice("device1")
        pool = ResourcePool("device1", [device])

        registry = ResourceRegistry()
        t1 = create_test_transporter("arm1", ["pad1", "pad2"])
        t2 = create_test_transporter("arm2", ["pad2", "device1"])
        registry.add_resource(t1)
        registry.add_resource(t2)
        registry.add_resource(device)
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"device1": device}, pads=["pad1", "pad2"],
        )

        plate = create_test_plate_template("plate")

        executed: list[str] = []

        @orca.action(device=pool, inputs=[plate])
        async def action_at_device(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)
            executed.append("ran")

        @orca.method
        async def method_at_device(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield action_at_device

        builder = SdkToSystemBuilder(
            name="multihop_test",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=method_at_device,
            labware_start_mapping={plate: "pad1"},
            labware_end_mapping={plate: "pad1"},
            system=system,
        )
        # The labware routes pad1->pad2 (arm1) then pad2->device1 (arm2).
        await executor.start()

        assert executed == ["ran"], "multi-hop routing must deliver labware so the action runs"

    @pytest.mark.asyncio
    async def test_labware_returns_via_multi_hop_after_action(self) -> None:
        """After action completes, labware returns to start via multi-hop.

        Topology: pad1 -- pad2 -- device1
        Thread starts at pad1, action at device1, end at pad1.
        Round trip: pad1->pad2->device1 (action) device1->pad2->pad1.
        """
        device = UniversalMockDevice("device1")
        pool = ResourcePool("device1", [device])

        registry = ResourceRegistry()
        t1 = create_test_transporter("arm1", ["pad1", "pad2"])
        t2 = create_test_transporter("arm2", ["pad2", "device1"])
        registry.add_resource(t1)
        registry.add_resource(t2)
        registry.add_resource(device)
        registry.add_resource_pool(pool)

        system_map = SystemMap(registry)
        await wire_system_map(
            system_map, devices={"device1": device}, pads=["pad1", "pad2"],
        )

        plate = create_test_plate_template("plate")

        executed: list[str] = []

        @orca.action(device=pool, inputs=[plate])
        async def action_at_device(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)
            executed.append("ran")

        @orca.method
        async def method_at_device(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
            yield action_at_device

        builder = SdkToSystemBuilder(
            name="multihop_return_test",
            description="",
            labwares=[plate],
            resources_registry=registry,
            system_map=system_map,
        )
        await builder.bind_labwares()
        system = builder.get_system()

        executor = StandaloneMethodExecutor(
            template=method_at_device,
            labware_start_mapping={plate: "pad1"},
            labware_end_mapping={plate: "pad1"},
            system=system,
        )
        # Round trip via multi-hop: pad1->pad2->device1->pad2->pad1
        await executor.start()

        assert executed == ["ran"], "action runs after multi-hop delivery; labware returns via multi-hop"
