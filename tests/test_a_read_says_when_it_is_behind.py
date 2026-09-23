"""A read whose operations are still on an unfinished action does not say known.

Every device op writes its record the moment the call returns, but the records
ride on the action until it ends. On the bench an action picked eight tips up
and died at its dispense, so `get-mounted-tips` folded a store that had heard
about neither: it answered `mounted: []` with `provenance: known` while eight
tips sat on the head.

Being behind is unavoidable. Saying `known` while behind is not, so a read
the tip pre-flight read the ledger, so a confident wrong answer defeats a safety
check rather than merely misinforming a person.

The numbers stay what the record says. An unfinished action may still be retried,
and folding its operations in early would double-count them -- a different wrong
answer. Only the confidence changes.
"""

import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.status_enums import RecoveryDecision
from orca.state.contents import LabwareContentsLedger
from orca.state.identity import LabwareRef
from orca.state.jsonl_store import JsonlOpsHistoryStore
from orca.state.mounted import MountedTipsLedger
from orca.state.ops_history import OpsHistory
from orca.state.projections import (
    contents_provenance,
    unsettled_gaps,
    went_unobserved,
)
from orca.state.provenance import Provenance
from orca.state.records import (
    DeviceOperation,
    InitialStateDetails,
    ObservationGapCause,
    ObservationGapDetails,
    OperationRecord,
    TipPickUpDetails,
    TrackingRecord,
    TrackingSource,
)
from orca.state.unrecorded import UnrecordedOperations
from tests.test_deck_resident_reagent_pipetting import build_pipetting_bench
from tests.test_helpers import run_to_quiescence, wait_for_paused_thread

_LH = "lh_1"
_RACK = "tips_96"
_RACK_ID = "id-of-tips-96"


class _ActionHoldingOperations:
    """An action that has done things and not been folded yet.

    Stands in for `ActionBodyLocationAction`, which is what the engine watches;
    the registry only ever asks for the operations.
    """

    def __init__(self, operations: list[OperationRecord]) -> None:
        self._operations = operations

    @property
    def pending_operations(self) -> list[OperationRecord]:
        return self._operations


def _picked(positions: list[str], at: float) -> OperationRecord:
    return OperationRecord(
        operation=DeviceOperation.PICK_UP_TIPS,
        device_name=_LH,
        affected_labware=[_RACK],
        affected_labware_ids=[_RACK_ID],
        action_id="act-2",
        thread_id="t1",
        details=TipPickUpDetails(
            tip_rack=_RACK, positions=positions, use_channels=None,
        ),
        timestamp=at,
    )


async def _history_with_a_settled_rack() -> OpsHistory:
    """A rack the record has spoken about and a head it has watched, so both
    ledgers read KNOWN before anything is left unrecorded."""
    history = OpsHistory(store=JsonlOpsHistoryStore.ephemeral())
    await history.append_initial_state(
        _RACK,
        InitialStateDetails(
            labware=_RACK,
            tip_positions_present=["A1", "B1", "C1"],
            well_volumes=None,
        ),
        labware_id=_RACK_ID,
    )
    await history.append_record(TrackingRecord(
        execution_id=history.execution_id,
        action_id="act-1",
        thread_id="t1",
        method_id=None,
        source=TrackingSource.OBSERVED,
        timestamp=1.0,
        operations=[_picked(["A1"], at=1.0)],
    ))
    return history


@pytest.mark.asyncio
class TestTheHeadDoesNotClaimToKnow:

    async def test_a_settled_head_reads_known(self) -> None:
        """The control: without an unfinished action the answer is unchanged."""
        history = await _history_with_a_settled_rack()
        assert (await MountedTipsLedger(history).of(_LH)).provenance is Provenance.KNOWN

    async def test_a_head_an_unfinished_action_touched_reads_stale(self) -> None:
        """The bench shape: tips picked up, the next op failed, nothing folded."""
        history = await _history_with_a_settled_rack()
        action = _ActionHoldingOperations([_picked(["B1"], at=2.0)])
        history.unrecorded.watch("act-2", action)

        mounted = await MountedTipsLedger(history).of(_LH)

        assert mounted.provenance is Provenance.STALE, (
            "the record has not heard about the pick this action already made"
        )

    async def test_folding_the_action_makes_the_head_known_again(self) -> None:
        history = await _history_with_a_settled_rack()
        action = _ActionHoldingOperations([_picked(["B1"], at=2.0)])
        history.unrecorded.watch("act-2", action)
        history.unrecorded.forget("act-2")

        assert (await MountedTipsLedger(history).of(_LH)).provenance is Provenance.KNOWN

    async def test_another_devices_unfinished_action_says_nothing_about_this_head(
        self,
    ) -> None:
        history = await _history_with_a_settled_rack()
        elsewhere = _picked(["B1"], at=2.0)
        object.__setattr__(elsewhere, "device_name", "lh_2")
        action = _ActionHoldingOperations([elsewhere])
        history.unrecorded.watch("act-2", action)

        assert (await MountedTipsLedger(history).of(_LH)).provenance is Provenance.KNOWN


@pytest.mark.asyncio
class TestTheRackDoesNotClaimToKnow:

    async def test_a_settled_rack_reads_known(self) -> None:
        history = await _history_with_a_settled_rack()
        ledger = LabwareContentsLedger(history)
        contents = await ledger.of(LabwareRef(id=_RACK_ID, name=_RACK))
        assert contents.provenance is Provenance.KNOWN

    async def test_a_rack_an_unfinished_action_touched_reads_stale(self) -> None:
        """The 72-versus-64 disagreement: the pick that emptied those positions
        is on the action, so the rack still reads full and said so confidently."""
        history = await _history_with_a_settled_rack()
        ledger = LabwareContentsLedger(history)
        rack = LabwareRef(id=_RACK_ID, name=_RACK)
        settled = await ledger.of(rack)
        action = _ActionHoldingOperations([_picked(["B1"], at=2.0)])
        history.unrecorded.watch("act-2", action)

        contents = await ledger.of(rack)

        assert contents.provenance is Provenance.STALE
        assert contents.tip_positions_present == settled.tip_positions_present, (
            "the layout is unchanged; only the confidence moved"
        )

    async def test_another_labwares_unfinished_action_says_nothing_about_this_rack(
        self,
    ) -> None:
        history = await _history_with_a_settled_rack()
        elsewhere = _picked(["B1"], at=2.0)
        object.__setattr__(elsewhere, "affected_labware", ["some_other_rack"])
        action = _ActionHoldingOperations([elsewhere])
        history.unrecorded.watch("act-2", action)
        ledger = LabwareContentsLedger(history)

        contents = await ledger.of(LabwareRef(id=_RACK_ID, name=_RACK))
        assert contents.provenance is Provenance.KNOWN


@pytest.mark.asyncio
class TestWhichGapsLetAPickThrough:

    async def test_a_restart_is_a_stretch_a_hand_could_have_used(self) -> None:
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.RUNTIME_RESTART, labware_id=_RACK_ID,
        )
        ops = await history.ops_for(_RACK)

        assert contents_provenance(ops, _RACK) is Provenance.STALE
        assert went_unobserved(ops, _RACK)

    async def test_operations_dropped_by_an_abort_is_not(self) -> None:
        """The gap an abort writes says the machine lost its own record, with
        nobody near the deck. Leniency there would drive a head at a position
        the record calls empty on the chance somebody refilled it."""
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.OPERATIONS_DROPPED, labware_id=_RACK_ID,
        )
        ops = await history.ops_for(_RACK)

        assert contents_provenance(ops, _RACK) is Provenance.STALE, (
            "a person still has to look"
        )
        assert not went_unobserved(ops, _RACK), (
            "but the gate that lets a pick through keeps refusing"
        )

    async def test_a_restart_after_an_abort_does_not_bring_the_tips_back(
        self,
    ) -> None:
        """The lenient answer is about a rack a hand could have reloaded. A
        restart says nothing about the tips an abort discarded, so the rack
        stays refused until somebody counts it."""
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.OPERATIONS_DROPPED, labware_id=_RACK_ID,
        )
        await history.append_observation_gap(
            _RACK, ObservationGapCause.RUNTIME_RESTART, labware_id=_RACK_ID,
        )
        ops = await history.ops_for(_RACK)

        assert not went_unobserved(ops, _RACK)

    async def test_a_lenient_gap_plus_an_unfinished_action_still_refuses(
        self,
    ) -> None:
        """A restart says a hand could have reloaded the rack. An action holding
        picks says the record is behind by picks that really happened. Together
        the rack could be fuller or emptier than the fold, and a gate that lets
        work through needs better than that."""
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.RUNTIME_RESTART, labware_id=_RACK_ID,
        )
        ledger = LabwareContentsLedger(history)
        ops = await history.ops_for(_RACK)
        assert ledger.went_unobserved(ops, _RACK), "the restart alone is lenient"

        action = _ActionHoldingOperations([_picked(["B1"], at=3.0)])
        history.unrecorded.watch("act-mid-flight", action)

        assert not ledger.went_unobserved(ops, _RACK)

    async def test_counting_the_rack_settles_every_gap_standing_on_it(
        self,
    ) -> None:
        """One statement, two gaps. Clearing them one at a time would leave a
        rack refused forever with nothing an operator could do about it."""
        history = await _history_with_a_settled_rack()
        await history.append_observation_gap(
            _RACK, ObservationGapCause.OPERATIONS_DROPPED, labware_id=_RACK_ID,
        )
        await history.append_observation_gap(
            _RACK, ObservationGapCause.RUNTIME_RESTART, labware_id=_RACK_ID,
        )
        await history.append_set_tip_state(_RACK, ["A1", "B1"], labware_id=_RACK_ID)
        ops = await history.ops_for(_RACK)

        assert not unsettled_gaps(ops, _RACK)
        assert contents_provenance(ops, _RACK) is Provenance.KNOWN


class TestTheRegistryDoesNotOutliveItsActions:

    def test_an_action_nothing_holds_is_forgotten(self) -> None:
        """An aborted thread drops its action without folding anything. Held
        strongly, that entry would leave every later read stale forever."""
        registry = UnrecordedOperations()
        action = _ActionHoldingOperations([_picked(["B1"], at=2.0)])
        registry.watch("act-2", action)
        assert registry.touches_device(_LH)

        del action

        assert not registry.touches_device(_LH)

    def test_an_action_with_nothing_pending_touches_nothing(self) -> None:
        registry = UnrecordedOperations()
        action = _ActionHoldingOperations([])
        registry.watch("act-2", action)
        assert not registry.touches_device(_LH)
        assert not registry.touches_labware(_RACK)


class _DispenseFails(RecordingLiquidHandlerDriver):
    """Picks tips and aspirates for real, then dies at the dispense.

    The bench failure exactly: the two ops that happened are on the action, the
    action never ends, and nothing reaches the store.
    """

    async def dispense(self, request):  # noqa: ANN001 - mirrors the driver signature
        raise RuntimeError("Simulated dispense failure")


async def _gap_causes(labware) -> list[ObservationGapCause]:
    return [
        op.details.cause
        for op in await labware.ops()
        if isinstance(op.details, ObservationGapDetails)
    ]


async def _ids_by_template(runtime) -> dict[str, str]:
    return {lw.template_name: lw.id for lw in await runtime.labware.list_all()}


@pytest.mark.slow
@pytest.mark.asyncio
async def test_the_reads_at_a_paused_action_all_admit_they_are_behind() -> None:
    """Pick tips up, fail the next op, then read everything an operator reads.

    Four surfaces answer about state the failed action moved and the record has
    not been told about: the head, the rack, the reservoir and the labware
    snapshot. Before this they answered `known`, and the tip pre-flight acts on
    that answer.
    """
    driver = _DispenseFails(ChatterboxLiquidHandlerDriver(num_channels=8))
    # The reservoir needs a declared starting volume or it reads UNKNOWN
    # whatever happens, and the assertion below would hold with no fix at all.
    build, _ = await build_pipetting_bench(driver, reservoir_volume=1000.0)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        # The bench head had been described before the failing action ran, which
        # is why it answered `known`. Without a baseline it reads `unknown`,
        # which is already unsettled and would pass with no fix at all.
        await runtime.devices.set_mounted_tips(
            "lh", {}, reason="the head is bare", confirm=True,
        )
        assert (await runtime.devices.get_mounted_tips("lh")).provenance is (
            Provenance.KNOWN
        )
        record = await runtime.submit_workflow(
            "pipetting_wf", mode=WorkflowRunMode.PURE_SIM,
        )
        await wait_for_paused_thread(runtime, record.id, timeout=30.0)
        assert any(c.method == "pick_up_tips" for c in driver.calls), (
            "the test needs the pick to have really happened"
        )
        ids = await _ids_by_template(runtime)

        mounted = await runtime.devices.get_mounted_tips("lh")
        rack = await runtime.labware.get_tip_state(ids["tips"])
        reservoir = await runtime.labware.get_well_volumes(ids["reservoir"])
        snapshot = await runtime.labware.get_by_id(ids["tips"])

        assert mounted.provenance is Provenance.STALE, (
            "the head is carrying tips the record has not been told about"
        )
        assert rack.provenance is Provenance.STALE
        assert reservoir.provenance is Provenance.STALE, (
            "the aspirate that drew this reservoir down is on the action"
        )
        assert snapshot.contents_provenance is Provenance.STALE
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_finished_run_leaves_every_read_known() -> None:
    """The control for the test above, and the guard against answering `stale`
    forever: once the actions end, their operations are in the record."""
    driver = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await build_pipetting_bench(driver)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "pipetting_wf", mode=WorkflowRunMode.PURE_SIM,
        )
        await run_to_quiescence(runtime, record.id)
        ids = await _ids_by_template(runtime)

        assert (await runtime.devices.get_mounted_tips("lh")).provenance is (
            Provenance.KNOWN
        )
        assert (await runtime.labware.get_tip_state(ids["tips"])).provenance is (
            Provenance.KNOWN
        )
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_read_that_is_behind_is_not_a_rack_nobody_watched() -> None:
    """The pre-flight must not go lenient because a read is merely behind.

    Its stale branch exists for a rack nobody has watched, where a hand could
    have refilled it. An action still holding its operations is not that: nobody
    was at the deck, so a position the record calls empty is the only thing to
    go on and the pick has to be refused.
    """
    driver = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await build_pipetting_bench(driver)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "pipetting_wf", mode=WorkflowRunMode.PURE_SIM,
        )
        await run_to_quiescence(runtime, record.id)
        ids = await _ids_by_template(runtime)
        rack = next(
            lw for lw in runtime.system.labwares if lw.id == ids["tips"]
        )
        await runtime.labware.set_tip_state(
            ids["tips"], [], reason="the rack is empty", confirm=True,
        )
        assert await rack.missing_tip_positions(["A1"]) == ["A1"]
        assert await rack.contents_provenance() is Provenance.KNOWN

        history = runtime.system.ops_history
        action = _ActionHoldingOperations([_picked(["B1"], at=99.0)])
        object.__setattr__(action.pending_operations[0], "affected_labware", [rack.name])
        object.__setattr__(
            action.pending_operations[0], "affected_labware_ids", [rack.id],
        )
        history.unrecorded.watch("act-mid-flight", action)

        assert await rack.contents_provenance() is Provenance.STALE, (
            "the read is behind, and says so"
        )
        assert not await rack.went_unobserved(), (
            "being behind is not a stretch nobody watched, and the gate that "
            "lets a pick through must keep asking the narrower question"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_an_abort_leaves_the_reads_asking_to_be_looked_at() -> None:
    """An abort drops the action's operations instead of recording them.

    RETRY carries them into the next attempt and CONTINUE writes them, but an
    abort loses them, so the head really is carrying tips the record will never
    hear about. Going quietly back to `known` there is the bench report word for
    word, so the abort says a person has to look instead.
    """
    driver = _DispenseFails(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await build_pipetting_bench(driver, reservoir_volume=1000.0)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        await runtime.devices.set_mounted_tips(
            "lh", {}, reason="the head is bare", confirm=True,
        )
        record = await runtime.submit_workflow(
            "pipetting_wf", mode=WorkflowRunMode.PURE_SIM,
        )
        paused = await wait_for_paused_thread(runtime, record.id, timeout=30.0)
        ids = await _ids_by_template(runtime)

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_THREAD)
        await run_to_quiescence(runtime, record.id)

        assert (await runtime.devices.get_mounted_tips("lh")).provenance is (
            Provenance.STALE
        ), "the head kept the tips the abort threw the record of away"
        assert (await runtime.labware.get_tip_state(ids["tips"])).provenance is (
            Provenance.STALE
        )

        rack = next(lw for lw in runtime.system.labwares if lw.id == ids["tips"])
        assert ObservationGapCause.OPERATIONS_DROPPED in await _gap_causes(rack), (
            "the loss is written down, so it outlives the action object rather "
            "than disappearing with it"
        )

        plate = next(
            lw for lw in runtime.system.labwares if lw.id == ids["sample_plate"]
        )
        assert ObservationGapCause.OPERATIONS_DROPPED not in await _gap_causes(plate), (
            "the dispense never ran, so nothing about the plate is wrong; a gap "
            "on every labware the action was configured with would send a "
            "person to count one that was never touched"
        )

        worklist = {s.subject: s for s in await runtime.unsettled_state()}
        assert "aborted" in worklist[rack.name].detail, (
            "the sentence an operator reads has to say an abort lost work, not "
            "that a stretch passed with nobody watching"
        )
        assert "set-tip-state" in worklist[rack.name].detail, (
            "and it names the verb that accepts the rack, not a confirm"
        )

        await runtime.labware.set_tip_state(
            ids["tips"], ["A1"], reason="counted by hand", confirm=True,
        )
        assert (await runtime.labware.get_tip_state(ids["tips"])).provenance is (
            Provenance.KNOWN
        ), "a person who looks can settle a written-down loss"
    finally:
        await runtime.shutdown()
