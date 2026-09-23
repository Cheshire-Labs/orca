"""Tip racks read from the record after a restart, and say when nobody has
looked since.

The gap this pins (state-ownership audit D2b, ruled 2026-08-24): a restarted
rack re-seeded from its template declaration (full again), silently disagreeing
with the recorded pick-ups, and nothing marked the rack as needing a human look.
Tips differ from volumes: the fold cannot see a hand pulling one while the
runtime is down, so a restart writes an observation gap and the rack reads stale
until an operator confirms or corrects it.
"""

import time
from collections.abc import AsyncGenerator

import pytest

from orca.resource_models.labware import LabwareInstance, TipRackInstance
from orca.state.contents import LabwareContentsLedger
from orca.state.projections import (
    Provenance,
    Source,
    has_tip_baseline,
    tip_layout,
    tips_present,
)
from orca.state.ops_history import OpsHistory
from orca.resource_models.resource_pool import ResourcePool
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    OperationRecord,
    ObservationGapCause,
    SetTipStateDetails,
    TipDrop96Details,
    TipPickUpDetails,
    TipPickUp96Details,
    TrackingRecord,
    TrackingSource,
)
from orca.runtime.danger import ConfirmationRequired
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.sim_labware import SimTipRackTemplate
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
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


def _op(
    operation: DeviceOperation,
    details: (
        InitialStateDetails | SetTipStateDetails | TipPickUpDetails
        | TipPickUp96Details | TipDrop96Details
    ),
    rack: str,
    timestamp: float,
) -> OperationRecord:
    return OperationRecord(
        operation=operation,
        device_name="lh",
        affected_labware=[rack],
        action_id="a",
        thread_id="t",
        details=details,
        timestamp=timestamp,
    )


def _seed(rack: str, positions: list[str], timestamp: float = 0.0) -> OperationRecord:
    return _op(
        DeviceOperation.INITIAL_STATE,
        InitialStateDetails(labware=rack, tip_positions_present=positions),
        rack, timestamp,
    )


def _pickup(rack: str, positions: list[str], timestamp: float) -> OperationRecord:
    return _op(
        DeviceOperation.PICK_UP_TIPS,
        TipPickUpDetails(tip_rack=rack, positions=positions),
        rack, timestamp,
    )


def _set_state(rack: str, positions: list[str], timestamp: float) -> OperationRecord:
    return _op(
        DeviceOperation.SET_TIP_STATE,
        SetTipStateDetails(labware=rack, tip_positions_present=positions),
        rack, timestamp,
    )


def _pickup96(rack: str, timestamp: float) -> OperationRecord:
    return _op(
        DeviceOperation.PICK_UP_TIPS96,
        TipPickUp96Details(tip_rack=rack),
        rack, timestamp,
    )


def _drop96_to_rack(rack: str, timestamp: float) -> OperationRecord:
    return _op(
        DeviceOperation.DROP_TIPS96,
        TipDrop96Details(tip_rack=rack, to_waste=False),
        rack, timestamp,
    )


class TestSetTipStateFold:
    def test_96_drop_back_restores_an_operator_set_baseline(self) -> None:
        """set -> 96 pickup -> 96 drop-to-rack must fold back to the set
        layout: the restore honors any baseline kind, not only INITIAL_STATE,
        else an operator-set rack folds to empty on the drop-back."""
        ops = [
            _set_state("r", ["A1", "A2"], 0.0),
            _pickup96("r", 1.0),
            _drop96_to_rack("r", 2.0),
        ]
        assert tips_present(ops, "r") == {"A1", "A2"}

    def test_96_drop_back_restores_the_latest_baseline(self) -> None:
        """A later operator set supersedes the initial seed for the restore."""
        ops = [
            _seed("r", ["A1", "A2", "A3"]),
            _set_state("r", ["A1"], 1.0),
            _pickup96("r", 2.0),
            _drop96_to_rack("r", 3.0),
        ]
        assert tips_present(ops, "r") == {"A1"}

    def test_set_state_is_an_absolute_overwrite(self) -> None:
        ops = [
            _seed("r", ["A1", "A2", "A3"]),
            _pickup("r", ["A1"], 1.0),
            _set_state("r", ["A1", "A2"], 2.0),
        ]
        assert tips_present(ops, "r") == {"A1", "A2"}

    def test_pickup_after_set_state_subtracts_from_it(self) -> None:
        ops = [
            _set_state("r", ["A1", "A2"], 0.0),
            _pickup("r", ["A2"], 1.0),
        ]
        assert tips_present(ops, "r") == {"A1"}

    def test_set_state_counts_as_a_baseline(self) -> None:
        ops = [_set_state("r", ["A1"], 0.0)]
        assert has_tip_baseline(ops, "r")


class TestHistoryTipState:
    def test_no_baseline_reads_as_no_history(self) -> None:
        """A pickup with no baseline gives the fold nothing to subtract from;
        seeding off it would assert an empty rack the system never saw."""
        ops = [_pickup("r", ["A1"], 1.0)]
        assert tip_layout(ops, "r") is None

    def test_projects_present_and_picked_positions(self) -> None:
        ops = [_seed("r", ["A1", "A2"]), _pickup("r", ["A1"], 1.0)]
        assert tip_layout(ops, "r") == {"A1": False, "A2": True}

    def test_operator_set_defines_the_universe(self) -> None:
        ops = [_set_state("r", ["B1"], 0.0)]
        assert tip_layout(ops, "r") == {"B1": True}


async def _build_system_with_rack() -> ISystem:
    """One device, one transporter, two pads, one plate workflow, plus a tip
    rack template registered for the operator surfaces."""
    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate = create_test_plate_template("plate_96")
    rack = SimTipRackTemplate("tips_96")

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

    @orca.action(device=pool, inputs=[rack])
    async def rack_touch(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def rack_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield rack_touch

    pad_loc = system_map.get_location("pad1")
    pad2_loc = system_map.get_location("pad2")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method

    # Every labware template needs a tracking thread; the rack's never runs
    # in these tests, it exists so `tips_96` registers on the system.
    @orca.thread(labware=rack, start=pad2_loc, end=pad2_loc)
    async def rack_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield rack_method

    workflow = WorkflowTemplate("simple_workflow")
    workflow.add_thread(plate_thread, is_start=True)
    workflow.add_thread(rack_thread)

    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate, rack],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    return builder.get_system()


class TestSeedFromHistory:
    async def test_driver_seed_folds_recorded_pickups(self) -> None:
        system = await _build_system_with_rack()
        template = system.get_labware_template("tips_96")
        instance = await template.create_instance()
        history = OpsHistory(execution_id="exec_1")
        await history.append_initial_state(
            instance.name,
            InitialStateDetails(
                labware=instance.name, tip_positions_present=["A1", "A2"],
            ),
        )
        await history.append_record(TrackingRecord(
            execution_id="exec_1", action_id="a", thread_id="t", method_id=None,
            source=TrackingSource.DECLARED, timestamp=time.time(),
            operations=[_pickup(instance.name, ["A1"], time.time())],
        ))
        instance.bind_contents(LabwareContentsLedger(history))

        state = await instance.driver_well_state()
        assert state is not None
        assert state.tips == {"A1": False, "A2": True}

    async def test_no_record_projects_nothing_rather_than_the_declaration(self) -> None:
        system = await _build_system_with_rack()
        template = system.get_labware_template("tips_96")
        instance = await template.create_instance()
        assert isinstance(instance, LabwareInstance)

        instance.bind_contents(LabwareContentsLedger(OpsHistory()))
        state = await instance.driver_well_state()
        assert state is None  # nothing has said what it holds; no claim rides the wire


class TestNeedingALook:
    """Which labware is worth interrupting an operator about, and when.

    Was a boolean set on the instance at restore; it is now derived from the
    record, so it survives a restart and cannot disagree with the number beside
    it. Two consequences worth stating: a labware nobody has ever described
    reads UNKNOWN rather than "confirmed empty", and a plate is treated like a
    rack -- the earlier rule flagged only racks, but a hand can change a plate's
    volumes across the same downtime, and the prompt is non-blocking either way.
    """

    async def _registered(self, template_name: str):
        system = await _build_system_with_rack()
        runtime = SystemRuntime(system, labware_store=InMemoryLabwareStore())
        await runtime.start()
        snap = await runtime.labware.register(template_name, confirm=True)
        instance = next(i for i in system.labwares if i.id == snap.id)
        return system, runtime, instance

    async def test_a_fresh_labware_needs_no_look(self) -> None:
        system, runtime, instance = await self._registered("tips_96")
        try:
            contents = await system.labware_contents.of(instance.ref)
            assert contents.provenance is Provenance.KNOWN
        finally:
            await runtime.shutdown()

    async def test_an_unobserved_stretch_makes_a_rack_worth_a_look(self) -> None:
        """The downtime is the uncertainty window: while nobody was watching,
        a hand could have pulled or replaced tips with nothing recording it."""
        system, runtime, instance = await self._registered("tips_96")
        try:
            await system.labware_contents.note_observation_gap(
                instance, ObservationGapCause.RUNTIME_RESTART,
            )
            contents = await system.labware_contents.of(instance.ref)
            assert contents.provenance is Provenance.STALE
        finally:
            await runtime.shutdown()

    async def test_a_plate_is_treated_the_same_way(self) -> None:
        """A plate whose template stated its volumes goes stale like a rack.

        Stated is the operative word: a plate template that declares nothing
        writes no opening entry, so there is no attestation for a gap to expire
        and the read stays unknown. That case is the test below.
        """
        system, runtime, instance = await self._registered("plate_96")
        try:
            await system.labware_contents.assert_volumes(instance, {"A1": 50.0})
            await system.labware_contents.note_observation_gap(
                instance, ObservationGapCause.RUNTIME_RESTART,
            )
            contents = await system.labware_contents.of(instance.ref)
            assert contents.provenance is Provenance.STALE
        finally:
            await runtime.shutdown()

    async def test_a_labware_nobody_described_is_unknown_not_stale(self) -> None:
        """Nothing to expire, so nothing to ask about: the gap is a no-op and
        the read says it knows nothing rather than claiming an empty labware."""
        system = await _build_system_with_rack()
        template = system.get_labware_template("tips_96")
        instance = await template.create_instance()
        system.add_labware(instance)
        await system.labware_contents.note_observation_gap(
            instance, ObservationGapCause.RUNTIME_RESTART,
        )
        contents = await system.labware_contents.of(instance.ref)
        assert contents.provenance is Provenance.UNKNOWN


class _RowMintingStore(InMemoryLabwareStore):
    """Returns identity-only rows per lookup, like a hosted deployment's DbLabwareStore."""

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


class TestOperatorVerbs:
    async def _started(self, store: InMemoryLabwareStore | None = None):
        system = await _build_system_with_rack()
        runtime = SystemRuntime(system, labware_store=store or InMemoryLabwareStore())
        await runtime.start()
        return system, runtime

    async def test_set_then_get_reflects_positions(self) -> None:
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await runtime.labware.set_tip_state(
                snap.id, ["A1", "B1"], reason="fresh rack, row A1/B1 only", confirm=True,
            )
            state = await runtime.labware.get_tip_state(snap.id)
            assert state.positions_present == ["A1", "B1"]
        finally:
            await runtime.shutdown()

    async def test_set_writes_operator_ledger_record(self) -> None:
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await runtime.labware.set_tip_state(
                snap.id, ["A1"], reason="r", confirm=True,
            )
            ops = await system.ops_history.ops_for(snap.name)
            set_ops = [o for o in ops if o.operation == DeviceOperation.SET_TIP_STATE]
            assert len(set_ops) == 1
            assert set_ops[0].source == TrackingSource.OPERATOR
            details = set_ops[0].details
            assert isinstance(details, SetTipStateDetails)
            assert details.tip_positions_present == ["A1"]
        finally:
            await runtime.shutdown()

    async def test_set_requires_confirm_and_reason(self) -> None:
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            with pytest.raises(ConfirmationRequired):
                await runtime.labware.set_tip_state(snap.id, ["A1"], reason="r")
            with pytest.raises(ValueError, match="reason"):
                await runtime.labware.set_tip_state(snap.id, ["A1"], confirm=True)
        finally:
            await runtime.shutdown()

    async def test_set_refuses_a_plate(self) -> None:
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("plate_96", confirm=True)
            with pytest.raises(ValueError, match="tip rack"):
                await runtime.labware.set_tip_state(
                    snap.id, ["A1"], reason="r", confirm=True,
                )
        finally:
            await runtime.shutdown()

    async def test_confirm_settles_a_stale_rack(self) -> None:
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await runtime.labware.set_tip_state(
                snap.id, ["A1"], reason="loaded", confirm=True,
            )
            instance = next(i for i in system.labwares if i.id == snap.id)
            await system.labware_contents.note_observation_gap(
                instance, ObservationGapCause.RUNTIME_RESTART,
            )
            await runtime.labware.confirm_tip_state(snap.id, confirm=True)
            state = await runtime.labware.get_tip_state(snap.id)
            assert state.provenance is Provenance.KNOWN
        finally:
            await runtime.shutdown()

    async def test_confirm_writes_the_projection_as_a_durable_baseline(self) -> None:
        """Confirming makes the agreed layout a ledger fact, so the next fold
        starts from what the operator saw, not from a seed a wipe could lose."""
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            await runtime.labware.set_tip_state(
                snap.id, ["A1", "A2"], reason="r", confirm=True,
            )
            await runtime.labware.confirm_tip_state(snap.id, confirm=True)
            ops = await system.ops_history.ops_for(snap.name)
            set_ops = [o for o in ops if o.operation == DeviceOperation.SET_TIP_STATE]
            assert len(set_ops) == 2
            details = set_ops[-1].details
            assert isinstance(details, SetTipStateDetails)
            assert details.tip_positions_present == ["A1", "A2"]
        finally:
            await runtime.shutdown()

    async def test_confirm_refuses_a_rack_the_record_knows_nothing_about(self) -> None:
        """Confirm means "the rack matches what you showed me". With nothing to
        show, it would record an empty rack nobody stated -- so it refuses and
        points at set_tip_state, which is how a layout gets asserted.

        Registering a rack now writes its opening entry, so reaching this state
        takes a labware that skipped the registry entirely.
        """
        system, runtime = await self._started()
        try:
            template = system.get_labware_template("tips_96")
            instance = await template.create_instance()
            system.add_labware(instance)
            state = await runtime.labware.get_tip_state(instance.id)
            assert state.positions_present == []
            assert state.provenance is Provenance.UNKNOWN
            with pytest.raises(ValueError, match="set_tip_state"):
                await runtime.labware.confirm_tip_state(instance.id, confirm=True)
            ops = await system.ops_history.ops_for(instance.name)
            assert not [
                o for o in ops if o.operation == DeviceOperation.SET_TIP_STATE
            ]
        finally:
            await runtime.shutdown()

    async def test_confirm_agrees_with_what_the_read_surface_showed(self) -> None:
        """The other half: a registered rack HAS a layout to agree with, and
        confirming records exactly the layout the read surface displayed."""
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            shown = (await runtime.labware.get_tip_state(snap.id)).positions_present
            await runtime.labware.confirm_tip_state(snap.id, confirm=True)
            ops = await system.ops_history.ops_for(snap.name)
            recorded = [
                o for o in ops if o.operation == DeviceOperation.SET_TIP_STATE
            ]
            assert len(recorded) == 1
            details = recorded[0].details
            assert isinstance(details, SetTipStateDetails)
            assert details.tip_positions_present == shown
        finally:
            await runtime.shutdown()

    async def test_set_settles_a_stale_rack(self) -> None:
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            instance = next(i for i in system.labwares if i.id == snap.id)
            await system.labware_contents.note_observation_gap(
                instance, ObservationGapCause.RUNTIME_RESTART,
            )
            await runtime.labware.set_tip_state(
                snap.id, ["A1"], reason="r", confirm=True,
            )
            state = await runtime.labware.get_tip_state(snap.id)
            assert state.provenance is Provenance.KNOWN
        finally:
            await runtime.shutdown()

    async def test_snapshot_reads_stale_after_a_gap(self) -> None:
        """A restart nobody watched leaves the fold intact and the rack stale,
        which is what puts it in front of an operator.

        Registering answers from the declaration, not from a person:
        `confirm=True` acknowledges the danger gate, it is not somebody saying
        what is in the rack. That is `source`, which is why provenance does not
        need a second member for it."""
        system, runtime = await self._started()
        try:
            snap = await runtime.labware.register("tips_96", confirm=True)
            assert snap.contents_provenance is Provenance.KNOWN
            resolved = await runtime.labware.resolve_contents(snap.id)
            assert resolved.source is Source.DECLARATION
            instance = next(i for i in system.labwares if i.id == snap.id)
            await system.labware_contents.note_observation_gap(
                instance, ObservationGapCause.RUNTIME_RESTART,
            )
            refreshed = await runtime.labware.get_by_id(snap.id)
            assert refreshed.contents_provenance is Provenance.STALE
        finally:
            await runtime.shutdown()


class TestRackSurvivesRestart:
    async def test_restarted_rack_seeds_from_history_and_reads_stale(self) -> None:
        """The incident shape: run picks tips, restart, boot. The rack must
        come back at its folded layout and flagged for a human look."""
        labware_store = _RowMintingStore()
        ops_store = JsonlOpsHistoryStore.ephemeral()

        system_a = await _build_system_with_rack()
        system_a.ops_history.bind_store(ops_store)
        runtime_a = SystemRuntime(system_a, labware_store=labware_store)
        await runtime_a.start()
        snap = await runtime_a.labware.register(
            "tips_96", location="pad1", confirm=True,
        )
        await runtime_a.labware.set_tip_state(
            snap.id, ["A1", "A2"], reason="loaded", confirm=True,
        )
        await runtime_a.shutdown()

        system_b = await _build_system_with_rack()
        system_b.ops_history.bind_store(ops_store)
        runtime_b = SystemRuntime(system_b, labware_store=labware_store)
        await runtime_b.start()
        try:
            state = await runtime_b.labware.get_tip_state(snap.id)
            assert state.positions_present == ["A1", "A2"]
            assert state.provenance is Provenance.STALE

            instance = next(i for i in system_b.labwares if i.id == snap.id)
            template = system_b.get_labware_template("tips_96")
            driver_state = await instance.driver_well_state()
            assert driver_state is not None
            assert driver_state.tips == {"A1": True, "A2": True}

            await runtime_b.labware.confirm_tip_state(snap.id, confirm=True)
            state = await runtime_b.labware.get_tip_state(snap.id)
            assert state.provenance is Provenance.KNOWN
        finally:
            await runtime_b.shutdown()
