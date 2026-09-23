"""What a labware holds is owned by the ops ledger, and survives both a second
execution and a restart.

Two bench failures on 2026-08-27 shared one root: a labware only ever got an
opening ledger entry when a thread minted it fresh. A rack the operator
registered, or one adopted from the store by a reuse-bound thread, had none. So
the fold had nothing to subtract picks from, and the same rack read as EMPTY on
the operator surface while being pushed to the instrument as FULL.

The rule these pin: every labware gets its opening entry once, at birth, on
every route into the system, and the template declaration is only ever that
opening entry. It is never a read-time fallback, so nothing can re-mint a
consumed rack as full.
"""

import time

import pytest
from collections.abc import AsyncGenerator

from orca.resource_models.labware import (
    ContentsUnbound,
    LabwareInstance,
    TipRackInstance,
)
from orca.state.ops_history import OpsHistory
from orca.resource_models.resource_pool import ResourcePool
from orca.state.records import (
    AspirateDetails,
    DeviceOperation,
    LabwareInitialState,
    OperationRecord,
    TipPickUpDetails,
    TrackingRecord,
    TrackingSource,
)
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.sim_labware import SimPlateTemplate, SimTipRackTemplate
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
import orca.orca as orca
from tests.test_helpers import (
    create_test_device,
    create_test_transporter,
    wire_system_map,
)


_RACK_POSITIONS = ["A1", "B1", "A2", "B2"]
_COLUMN_1 = ["A1", "B1"]
_PLATE_WELLS = {"A1": 100.0, "A2": 100.0}


class _DeclaredTipRackTemplate(SimTipRackTemplate):
    """A sim rack whose declared layout is explicit.

    ``SimTipRack`` materializes tip spots lazily, so a plain sim rack enumerates
    none and its declaration would be silently empty. Naming the positions keeps
    these tests about the ledger rather than about sim geometry.
    """

    def __init__(self, name: str, positions: list[str]) -> None:
        super().__init__(name)
        self._initial_state = LabwareInitialState(tip_positions=list(positions))


class _DeclaredPlateTemplate(SimPlateTemplate):
    def __init__(self, name: str, wells: dict[str, float]) -> None:
        super().__init__(name)
        self._initial_state = LabwareInitialState(wells=dict(wells))


class _RowMintingStore(InMemoryLabwareStore):
    """Hands back identity-only rows, the way a hosted deployment's Db-backed store does."""

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        stored = await super().get_by_id(labware_id)
        if stored is None:
            return None
        return LabwareInstance(
            template_name=stored.template_name,
            labware_type=stored.labware_type,
            barcode=stored.barcode,
            instance_id=stored.id,
            name=stored.name,
        )


async def _build_system() -> ISystem:
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate = _DeclaredPlateTemplate("plate_96", _PLATE_WELLS)
    rack = _DeclaredTipRackTemplate("tips_96", _RACK_POSITIONS)

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    registry.add_resource_pool(ResourcePool("shaker1", [device]))

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])
    pool = registry.get_resource_pool("shaker1")

    @orca.action(device=pool, inputs=[plate])
    async def touch_plate(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def plate_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield touch_plate

    @orca.action(device=pool, inputs=[rack])
    async def touch_rack(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def rack_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield touch_rack

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate, start=pad1, end=pad1)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield plate_method

    @orca.thread(labware=rack, start=pad2, end=pad2)
    async def rack_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield rack_method

    workflow = WorkflowTemplate("contents_workflow")
    workflow.add_thread(plate_thread, is_start=True)
    workflow.add_thread(rack_thread)

    builder = SdkToSystemBuilder(
        name="contents_system",
        description="",
        labwares=[plate, rack],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


async def _record_pick_up(
    system: ISystem, name: str, labware_id: str, positions: list[str],
    execution_id: str = "exec_1",
) -> None:
    """Write the pick-up a liquid handler would have recorded."""
    history: OpsHistory = system.ops_history.for_execution(execution_id)
    now = time.time()
    await history.append_record(TrackingRecord(
        execution_id=execution_id, action_id="a", thread_id="t", method_id=None,
        source=TrackingSource.OBSERVED, timestamp=now,
        operations=[OperationRecord(
            operation=DeviceOperation.PICK_UP_TIPS,
            device_name="lh",
            affected_labware=[name],
            affected_labware_ids=[labware_id],
            action_id="a", thread_id="t",
            details=TipPickUpDetails(tip_rack=name, positions=list(positions)),
            timestamp=now,
        )],
    ))


async def _record_aspirate(
    system: ISystem, name: str, labware_id: str, wells: dict[str, float],
    execution_id: str = "exec_1",
) -> None:
    history: OpsHistory = system.ops_history.for_execution(execution_id)
    now = time.time()
    await history.append_record(TrackingRecord(
        execution_id=execution_id, action_id="a", thread_id="t", method_id=None,
        source=TrackingSource.OBSERVED, timestamp=now,
        operations=[OperationRecord(
            operation=DeviceOperation.ASPIRATE,
            device_name="lh",
            affected_labware=[name],
            affected_labware_ids=[labware_id],
            action_id="a", thread_id="t",
            details=AspirateDetails(
                labware=name, positions=list(wells), volumes=list(wells.values()),
            ),
            timestamp=now,
        )],
    ))


async def _started(
    labware_store: InMemoryLabwareStore | None = None,
    ops_store: JsonlOpsHistoryStore | None = None,
) -> tuple[ISystem, SystemRuntime]:
    system = await _build_system()
    if ops_store is not None:
        system.ops_history.bind_store(ops_store)
    runtime = SystemRuntime(
        system, labware_store=labware_store or InMemoryLabwareStore(),
    )
    await runtime.start()
    return system, runtime


class TestBirthWritesTheOpeningEntry:
    """Every route into the system writes the declared layout to the ledger."""

    async def test_a_registered_rack_reads_as_its_declared_layout(self) -> None:
        """The operator put a full rack down; the read surface must say so.

        Before the fix this read ``[]`` -- not "I do not know", but a confident
        empty rack -- because ``register`` never wrote an opening entry.
        """
        _system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            state = await runtime.labware.get_tip_state(snap.id)
            assert sorted(state.positions_present) == sorted(_RACK_POSITIONS)
        finally:
            await runtime.shutdown()

    async def test_a_registered_plate_reads_as_its_declared_volumes(self) -> None:
        _system, runtime = await _started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            volumes = (await runtime.labware.get_well_volumes(snap.id)).volumes
            assert volumes == _PLATE_WELLS
        finally:
            await runtime.shutdown()

    async def test_the_opening_entry_is_written_once(self) -> None:
        """A second birth-time seed would reset a consumed rack to full."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(system, snap.name, snap.id, _COLUMN_1)
            await runtime.labware.register("tips_96", confirm=True)
            state = await runtime.labware.get_tip_state(snap.id)
            assert sorted(state.positions_present) == ["A2", "B2"]
        finally:
            await runtime.shutdown()


class TestASecondRunSeesTheFirstRunsPicks:
    """Bench failure 2: same process, second execution, rack read as empty."""

    async def test_a_pick_is_visible_on_the_next_read(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(system, snap.name, snap.id, _COLUMN_1)
            state = await runtime.labware.get_tip_state(snap.id)
            assert sorted(state.positions_present) == ["A2", "B2"]
        finally:
            await runtime.shutdown()

    async def test_a_later_execution_folds_the_earlier_one(self) -> None:
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(
                system, snap.name, snap.id, ["A1"], execution_id="run_1",
            )
            await _record_pick_up(
                system, snap.name, snap.id, ["B1"], execution_id="run_2",
            )
            state = await runtime.labware.get_tip_state(snap.id)
            assert sorted(state.positions_present) == ["A2", "B2"]
        finally:
            await runtime.shutdown()

    async def test_the_deck_projection_agrees_with_the_read(self) -> None:
        """One physical rack, two live views: they must not be able to differ."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(system, snap.name, snap.id, _COLUMN_1)
            state = await runtime.labware.get_tip_state(snap.id)

            instance = next(i for i in system.labwares if i.id == snap.id)
            projected = await instance.driver_well_state()

            assert projected is not None and projected.tips is not None
            present = sorted(p for p, held in projected.tips.items() if held)
            assert present == sorted(state.positions_present)
        finally:
            await runtime.shutdown()


class TestARestartKeepsTheLayout:
    """Bench failure 1: after a restart the rack came back full."""

    async def test_a_restarted_rack_keeps_its_consumed_layout(self) -> None:
        labware_store = _RowMintingStore()
        ops_store = JsonlOpsHistoryStore.ephemeral()

        system_a, runtime_a = await _started(labware_store, ops_store)
        snap = await runtime_a.labware.register(
            "tips_96", location="pad1", confirm=True,
        )
        await _record_pick_up(system_a, snap.name, snap.id, _COLUMN_1)
        await runtime_a.shutdown()

        system_b, runtime_b = await _started(labware_store, ops_store)
        try:
            state = await runtime_b.labware.get_tip_state(snap.id)
            assert sorted(state.positions_present) == ["A2", "B2"]

            instance = next(i for i in system_b.labwares if i.id == snap.id)
            projected = await instance.driver_well_state()
            assert projected is not None and projected.tips is not None
            present = sorted(p for p, held in projected.tips.items() if held)
            assert present == ["A2", "B2"]
        finally:
            await runtime_b.shutdown()

    async def test_a_restarted_plate_keeps_its_drawn_down_volumes(self) -> None:
        labware_store = _RowMintingStore()
        ops_store = JsonlOpsHistoryStore.ephemeral()

        system_a, runtime_a = await _started(labware_store, ops_store)
        snap = await runtime_a.labware.register(
            "plate_96", location="pad1", confirm=True,
        )
        await _record_aspirate(system_a, snap.name, snap.id, {"A1": 30.0})
        await runtime_a.shutdown()

        _system_b, runtime_b = await _started(labware_store, ops_store)
        try:
            volumes = (await runtime_b.labware.get_well_volumes(snap.id)).volumes
            assert volumes == {"A1": 70.0, "A2": 100.0}
        finally:
            await runtime_b.shutdown()


class TestTheDeclarationIsNeverAReadTimeFallback:
    async def test_a_rack_drained_to_empty_stays_empty(self) -> None:
        """Draining the last tip must not fall back to the declared full rack."""
        system, runtime = await _started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await _record_pick_up(system, snap.name, snap.id, _RACK_POSITIONS)
            state = await runtime.labware.get_tip_state(snap.id)
            assert state.positions_present == []

            instance = next(i for i in system.labwares if i.id == snap.id)
            projected = await instance.driver_well_state()
            assert projected is not None and projected.tips is not None
            assert not any(projected.tips.values())
        finally:
            await runtime.shutdown()


class TestASkippedSeedIsLoud:
    """The reason there is only one binder.

    A labware that reaches the driver before its opening entry exists gets the
    driver's own default, which for a tip rack is a full rack. That shipped
    three times, silently, because a second binder left the labware readable
    and it answered "nothing said" instead of raising. With one binder the
    fourth route to skip seeding fails here rather than at the instrument.
    """

    async def test_reading_an_unseeded_labware_raises(self) -> None:
        system, runtime = await _started()
        try:
            template = system.get_labware_template("tips_96")
            unseeded = await template.create_instance()
            system.add_labware(unseeded)

            with pytest.raises(ContentsUnbound):
                await unseeded.driver_well_state()
        finally:
            await runtime.shutdown()

    async def test_choosing_tips_on_an_unseeded_rack_raises(self) -> None:
        system, runtime = await _started()
        try:
            template = system.get_labware_template("tips_96")
            unseeded = await template.create_instance()
            system.add_labware(unseeded)
            assert isinstance(unseeded, TipRackInstance)

            with pytest.raises(ContentsUnbound):
                await unseeded.next_tips(1)
        finally:
            await runtime.shutdown()

    async def test_seeding_makes_both_readable(self) -> None:
        system, runtime = await _started()
        try:
            template = system.get_labware_template("tips_96")
            seeded = await template.create_instance()
            await seeded.enter_record(system.labware_contents)

            assert await seeded.driver_well_state() is not None
            assert isinstance(seeded, TipRackInstance)
            assert len(await seeded.next_tips(1)) == 1
        finally:
            await runtime.shutdown()
