"""What the blocker list reports, and what it refuses to report.

The bench case these pin: a PF400 initialize dropped its socket, which latched a
device fault; a clean re-initialize did not clear it; the next thread paused
when the arm refused. Two rows, two decisions, and nothing told the operator
there was a second one.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from orca.runtime.blockers import (
    BlockerKind,
    BlockerSeverity,
    derive_blockers,
    runtime_not_built_blocker,
    runtime_torn_down_blocker,
)
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentDetail,
    IncidentSeverity,
    OtherIncidentDetail,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import (
    DeviceFaultSummary,
    DeviceSnapshot,
    ExecutionDetail,
    PendingManualStepRecord,
    ReservationSnapshot,
    ThreadSnapshot,
)
from orca.system.reservation_manager.errors import ActionFailedContext
from orca.workflow_models.status_enums import HONOURED_DECISIONS, PauseSite


@dataclass
class FakeSources:
    devices: list[DeviceSnapshot] = field(default_factory=list)
    executions: list[ExecutionDetail] = field(default_factory=list)
    manual_steps: list[PendingManualStepRecord] = field(default_factory=list)
    reservations: list[ReservationSnapshot] = field(default_factory=list)
    incidents: list[SystemIncident] = field(default_factory=list)

    async def blocker_device_snapshots(self) -> list[DeviceSnapshot]:
        return self.devices

    def blocker_execution_details(self) -> list[ExecutionDetail]:
        return self.executions

    def blocker_pending_manual_steps(self) -> list[PendingManualStepRecord]:
        return self.manual_steps

    def blocker_reservations(self) -> list[ReservationSnapshot]:
        return self.reservations

    async def blocker_open_incidents(self) -> list[SystemIncident]:
        return self.incidents


def device(
    name: str,
    *,
    fault: DeviceFaultSummary | None = None,
    held: str | None = None,
) -> DeviceSnapshot:
    return DeviceSnapshot(
        name=name,
        type_name="Transporter",
        is_initialized=True,
        is_busy=False,
        effective_mode=WorkflowRunMode.LIVE,
        position_ids=(),
        loaded_labware_ids=(),
        under_external_control=held is not None,
        external_control_hold=held,
        fault=fault,
    )


def fault(command: str, *, outcome: str = "failed") -> DeviceFaultSummary:
    return DeviceFaultSummary(
        command=command,
        outcome=outcome,
        error="WinError 1236: the network connection was aborted",
        error_type="CommandExecutionError",
        at=1788278925.2,
        may_still_be_moving=outcome == "unknown",
        message=f"pf400_1: {command!r} did not come back clean.",
        execution_id="exec-1",
    )


def thread(
    thread_id: str,
    *,
    name: str = "r6_source",
    status: str = "PAUSED",
    pause_reason: str | None = None,
    pause_site: PauseSite | None = None,
    paused_device_command: str | None = None,
    waiting_for: str | None = None,
    last_error: str | None = None,
) -> ThreadSnapshot:
    return ThreadSnapshot(
        id=thread_id,
        name=name,
        status=status,
        current_location="pad_1",
        current_method=None,
        completed_method_count=0,
        last_error=last_error,
        pause_reason=pause_reason,
        completed_methods=(),
        labware_id=thread_id,
        labware_name=name,
        labware_template_name="sample_plate",
        paused_device_command=paused_device_command,
        pause_message="pick from pad_1 failed",
        pause_site=pause_site.value if pause_site is not None else None,
        # Populated the same way the real snapshot does it, so a fake can never
        # offer a verb the engine would refuse.
        honoured_decisions=(
            ()
            if pause_site is None
            else tuple(d.value for d in HONOURED_DECISIONS[pause_site].honours)
        ),
        waiting_for=waiting_for,
    )


def execution(
    threads: list[ThreadSnapshot],
    *,
    execution_id: str = "exec-1",
    paused: bool = False,
    pause_reason: str | None = None,
) -> ExecutionDetail:
    return ExecutionDetail(
        id=execution_id,
        workflow_name="smc_assay",
        status="ACCEPTING",
        error=None,
        threads=threads,
        total_thread_count=len(threads),
        completed_thread_count=0,
        active_thread_count=len(threads),
        paused=paused,
        pause_reason=pause_reason,
    )


def incident(
    category: IncidentCategory,
    *,
    execution_id: str = "exec-1",
    thread_id: str | None = None,
    detail: IncidentDetail | None = None,
    at: float | None = None,
) -> SystemIncident:
    return SystemIncident(
        id=f"inc-{category.value}-{thread_id or 'none'}",
        timestamp=time.time() if at is None else at,
        category=category,
        severity=IncidentSeverity.ERROR,
        execution_id=execution_id,
        thread_id=thread_id,
        message="something broke",
        detail=detail if detail is not None else OtherIncidentDetail(message_extra=""),
        recovery_action=RecoveryAction.NONE,
        acknowledged=False,
    )


def action_failed(error_type: str) -> ActionFailedContext:
    return ActionFailedContext(
        action_command="run_assay_step",
        method_name="condense",
        error_type=error_type,
        error_message="failed",
        device_command="aspirate",
    )


def verbs_of(blockers) -> set[str]:
    return {s.verb for b in blockers for r in b.remedies for s in r.steps}


@pytest.mark.asyncio
async def test_a_cleared_device_fault_leaves_the_paused_thread_on_the_list():
    """The bench case. Two rows, and clearing one must not empty the list."""
    sources = FakeSources(
        devices=[device("pf400_1", fault=fault("initialize"))],
        executions=[execution([
            thread("t1", pause_reason="error", pause_site=PauseSite.MOVE),
        ])],
    )

    before = await derive_blockers(sources)
    assert {b.kind for b in before} == {
        BlockerKind.DEVICE_FAULT, BlockerKind.THREAD_ERROR_PAUSE,
    }

    sources.devices = [device("pf400_1")]
    after = await derive_blockers(sources)
    assert [b.kind for b in after] == [BlockerKind.THREAD_ERROR_PAUSE]


@pytest.mark.asyncio
async def test_a_device_that_may_still_be_moving_sorts_above_everything():
    sources = FakeSources(devices=[
        device("flex_1", fault=fault("move_plate")),
        device("pf400_1", fault=fault("pick_plate", outcome="unknown")),
    ])
    blockers = await derive_blockers(sources)
    assert blockers[0].device_name == "pf400_1"
    assert blockers[0].may_still_be_moving is True


@pytest.mark.asyncio
async def test_a_move_pause_never_offers_the_verbs_that_kill_the_execution():
    """ABORT_ACTION and ABORT_METHOD re-raise at a move and fail the run."""
    sources = FakeSources(executions=[execution([
        thread("t1", pause_reason="error", pause_site=PauseSite.MOVE),
    ])])
    offered = verbs_of(await derive_blockers(sources))
    assert "thread.recover.ABORT_ACTION" not in offered
    assert "thread.recover.ABORT_METHOD" not in offered
    assert "thread.recover.RETRY" in offered
    assert "thread.recover.ABORT_THREAD" in offered


@pytest.mark.asyncio
async def test_the_op_level_retry_is_offered_only_inside_a_device_call():
    inside = FakeSources(executions=[execution([
        thread(
            "t1", pause_reason="error", pause_site=PauseSite.DEVICE_OP,
            paused_device_command="pick_plate",
        ),
    ])])
    outside = FakeSources(executions=[execution([
        thread("t1", pause_reason="error", pause_site=PauseSite.ACTION_BODY),
    ])])
    assert "thread.recover.RETRY_OP" in verbs_of(await derive_blockers(inside))
    assert "thread.recover.RETRY_OP" not in verbs_of(await derive_blockers(outside))


@pytest.mark.asyncio
async def test_an_incident_does_not_add_a_second_row_beside_its_thread():
    """The incident is the record of the pause, not a second thing to clear."""
    sources = FakeSources(
        executions=[execution([
            thread("t1", pause_reason="error", pause_site=PauseSite.ACTION_BODY),
        ])],
        incidents=[incident(IncidentCategory.ACTION_FAILED, thread_id="t1")],
    )
    blockers = await derive_blockers(sources)
    assert len(blockers) == 1
    assert blockers[0].incident_id == "inc-ACTION_FAILED-t1"


@pytest.mark.asyncio
async def test_a_thread_starved_by_a_parked_holder_is_reported():
    """Nothing is error-paused here, so every other read shows a healthy run."""
    sources = FakeSources(
        executions=[execution([
            thread("plate_1", name="plate_1", status="AWAITING_MANUAL_REMOVE",
                   waiting_for="flex_1/C4"),
            thread("plate_2", name="plate_2", status="AWAITING_ACTION_RESERVATION",
                   waiting_for="flex_1"),
        ])],
        reservations=[ReservationSnapshot(
            position_id="flex_1", reservation_id="rsv-1", thread_id="plate_1",
        )],
    )
    blockers = await derive_blockers(sources)
    starved = [b for b in blockers if b.kind is BlockerKind.WAITING_ON_A_PARKED_HOLDER]
    assert len(starved) == 1
    assert starved[0].thread_id == "plate_2"
    assert "plate_1" in starved[0].headline
    cancel = starved[0].remedies[0].steps[0]
    assert cancel.verb == "reservation.cancel"
    assert cancel.args["reservation_id"] == "rsv-1"


@pytest.mark.asyncio
async def test_the_parked_holder_says_which_waiter_it_releases():
    sources = FakeSources(
        executions=[execution([
            thread("plate_1", name="plate_1", status="AWAITING_MANUAL_REMOVE",
                   waiting_for="flex_1/C4"),
            thread("plate_2", name="plate_2", status="AWAITING_ACTION_RESERVATION",
                   waiting_for="flex_1"),
        ])],
        reservations=[ReservationSnapshot(
            position_id="flex_1", reservation_id="rsv-1", thread_id="plate_1",
        )],
    )
    blockers = await derive_blockers(sources)
    holder = next(b for b in blockers if b.kind is BlockerKind.MANUAL_REMOVE)
    starved = next(
        b for b in blockers if b.kind is BlockerKind.WAITING_ON_A_PARKED_HOLDER
    )
    assert holder.blocks == (starved.id,)


@pytest.mark.asyncio
async def test_ordinary_contention_is_not_a_blocker():
    """A holder that is still working releases on its own; waits last hours."""
    sources = FakeSources(
        executions=[execution([
            thread("plate_1", name="plate_1", status="EXECUTING_ACTION"),
            thread("plate_2", name="plate_2", status="AWAITING_ACTION_RESERVATION",
                   waiting_for="flex_1"),
        ])],
        reservations=[ReservationSnapshot(
            position_id="flex_1", reservation_id="rsv-1", thread_id="plate_1",
        )],
    )
    assert await derive_blockers(sources) == []


@pytest.mark.asyncio
async def test_a_named_remedy_wins_the_recommendation_from_the_bare_verbs():
    sources = FakeSources(
        executions=[execution([
            thread("t1", pause_reason="error", pause_site=PauseSite.DEVICE_OP,
                   paused_device_command="aspirate"),
        ])],
        incidents=[incident(
            IncidentCategory.ACTION_FAILED,
            thread_id="t1",
            detail=action_failed("TooLittleLiquidError"),
        )],
    )
    blocker = (await derive_blockers(sources))[0]
    recommended = [r for r in blocker.remedies if r.recommended]
    assert len(recommended) == 1
    assert recommended[0].id == "record_the_real_volume"
    assert [s.verb for s in recommended[0].steps] == [
        "labware.set_well_volumes", "thread.recover.RETRY_OP",
    ]


@pytest.mark.asyncio
async def test_an_unrecognised_failure_still_offers_the_bare_verbs():
    sources = FakeSources(
        executions=[execution([
            thread("t1", pause_reason="error", pause_site=PauseSite.ACTION_BODY),
        ])],
        incidents=[incident(
            IncidentCategory.ACTION_FAILED,
            thread_id="t1",
            detail=action_failed("SomethingNobodyHasSeenError"),
        )],
    )
    blocker = (await derive_blockers(sources))[0]
    recommended = [r for r in blocker.remedies if r.recommended]
    assert len(recommended) == 1
    assert recommended[0].id == "thread.recover.RETRY"


@pytest.mark.asyncio
async def test_a_manual_park_is_a_warning_and_a_broken_device_is_an_error():
    sources = FakeSources(
        devices=[device("pf400_1", fault=fault("initialize"))],
        executions=[execution([
            thread("t1", status="AWAITING_MANUAL_PLACE", waiting_for="pad_3"),
        ])],
    )
    by_kind = {b.kind: b for b in await derive_blockers(sources)}
    assert by_kind[BlockerKind.MANUAL_PLACE].severity is BlockerSeverity.WARNING
    assert by_kind[BlockerKind.DEVICE_FAULT].severity is BlockerSeverity.ERROR


@pytest.mark.asyncio
async def test_a_pending_manual_step_is_on_the_list():
    sources = FakeSources(manual_steps=[PendingManualStepRecord(
        execution_id="exec-1",
        step_id="step-1",
        instruction="Top up the DMSO reservoir",
        emitted_at=datetime.now(),
    )])
    blocker = (await derive_blockers(sources))[0]
    assert blocker.kind is BlockerKind.MANUAL_STEP
    assert blocker.headline == "Top up the DMSO reservoir"


@pytest.mark.asyncio
async def test_a_healthy_system_reports_nothing():
    assert await derive_blockers(FakeSources()) == []


def test_a_runtime_that_will_not_build_is_a_blocker_without_a_runtime():
    blocker = runtime_not_built_blocker(
        "KeyError", "Location dmso_reservoir does not exist", "Fix the workflow.",
    )
    assert blocker.kind is BlockerKind.RUNTIME_NOT_BUILT
    assert blocker.remedies[0].steps[0].verb == "runtime.reload"


@pytest.mark.asyncio
async def test_a_row_nobody_timestamped_says_unknown_rather_than_1970():
    """`since` is unix seconds on the wire, so 0.0 formats as January 1970."""
    sources = FakeSources(
        executions=[execution([
            thread("plate_1", name="plate_1", status="AWAITING_MANUAL_REMOVE",
                   waiting_for="flex_1/C4"),
            thread("plate_2", name="plate_2", status="AWAITING_ACTION_RESERVATION",
                   waiting_for="flex_1"),
        ])],
        reservations=[ReservationSnapshot(
            position_id="flex_1", reservation_id="rsv-1", thread_id="plate_1",
        )],
    )
    blockers = await derive_blockers(sources)
    starved = next(
        b for b in blockers if b.kind is BlockerKind.WAITING_ON_A_PARKED_HOLDER
    )
    assert starved.since is None


@pytest.mark.asyncio
async def test_a_dated_row_sorts_above_one_whose_start_nobody_recorded():
    sources = FakeSources(
        devices=[device("pf400_1", fault=fault("initialize"))],
        executions=[execution([
            thread("plate_1", name="plate_1", status="AWAITING_MANUAL_REMOVE",
                   waiting_for="flex_1/C4"),
        ])],
    )
    blockers = await derive_blockers(sources)
    manual = [b for b in blockers if b.kind is BlockerKind.MANUAL_REMOVE]
    assert manual and manual[0].since is None
    dated = [b for b in blockers if b.since is not None]
    assert dated, "the faulted device carries the time it latched"


@pytest.mark.asyncio
async def test_the_cancel_step_says_the_reason_rides_in_the_query_string():
    """A hosted deployment takes `reason` as a query parameter, so a JSON body gets a 400."""
    sources = FakeSources(
        executions=[execution([
            thread("plate_1", name="plate_1", status="AWAITING_MANUAL_REMOVE",
                   waiting_for="flex_1/C4"),
            thread("plate_2", name="plate_2", status="AWAITING_ACTION_RESERVATION",
                   waiting_for="flex_1"),
        ])],
        reservations=[ReservationSnapshot(
            position_id="flex_1", reservation_id="rsv-1", thread_id="plate_1",
        )],
    )
    blockers = await derive_blockers(sources)
    starved = next(
        b for b in blockers if b.kind is BlockerKind.WAITING_ON_A_PARKED_HOLDER
    )
    cancel = starved.remedies[0].steps[0]
    assert cancel.query == ("reason",)
    assert "{" not in cancel.rest, "the route must carry real ids, not placeholders"


def test_no_runtime_and_no_build_error_still_says_nothing_can_run():
    """A topology delete tears the runtime down on purpose.

    An empty list here would report a deployment that cannot run anything as
    free to move.
    """
    blocker = runtime_torn_down_blocker()
    assert blocker.kind is BlockerKind.RUNTIME_NOT_BUILT
    assert blocker.severity is BlockerSeverity.ERROR
    assert "no runtime" in blocker.headline.lower()
    assert blocker.remedies[0].steps[0].verb == "runtime.reload"


@pytest.mark.asyncio
async def test_two_rows_of_one_kind_put_the_dated_one_first():
    """Kind order decides across kinds, so only same-kind rows test `since`."""
    sources = FakeSources(
        executions=[execution([
            thread("undated", name="undated", pause_reason="error",
                   pause_site=PauseSite.MOVE),
            thread("dated", name="dated", pause_reason="error",
                   pause_site=PauseSite.MOVE),
        ])],
        incidents=[incident(
            IncidentCategory.ACTION_FAILED,
            thread_id="dated",
            detail=action_failed("SlotOccupiedError"),
        )],
    )
    blockers = await derive_blockers(sources)
    paused = [b for b in blockers if b.kind is BlockerKind.THREAD_ERROR_PAUSE]
    assert [b.thread_id for b in paused] == ["dated", "undated"]
    assert paused[0].since is not None
    assert paused[1].since is None


@pytest.mark.asyncio
async def test_the_other_cancel_step_also_says_the_reason_rides_in_the_url():
    """There are two reservation-cancel steps and both hit the same route."""
    sources = FakeSources(
        executions=[execution([
            thread("plate_1", name="plate_1", pause_reason="error",
                   pause_site=PauseSite.ACTION_BODY),
        ])],
        incidents=[incident(
            IncidentCategory.ACTION_FAILED,
            thread_id="plate_1",
            detail=action_failed("LocationReservedError"),
        )],
    )
    blockers = await derive_blockers(sources)
    steps = [
        step
        for b in blockers
        for r in b.remedies
        for step in r.steps
        if step.verb == "reservation.cancel"
    ]
    assert steps, "the position-already-reserved remedy offers a cancel"
    for step in steps:
        assert step.query == ("reason",)
        assert "{" not in step.rest


@pytest.mark.asyncio
async def test_the_older_of_two_dated_rows_comes_first():
    """The fourth sort term. Two rows of one kind, both dated, oldest first."""
    sources = FakeSources(
        executions=[execution([
            thread("newer", name="newer", pause_reason="error",
                   pause_site=PauseSite.MOVE),
            thread("older", name="older", pause_reason="error",
                   pause_site=PauseSite.MOVE),
        ])],
        incidents=[
            incident(IncidentCategory.ACTION_FAILED, thread_id="newer",
                     detail=action_failed("SlotOccupiedError"), at=2000.0),
            incident(IncidentCategory.ACTION_FAILED, thread_id="older",
                     detail=action_failed("SlotOccupiedError"), at=1000.0),
        ],
    )
    blockers = await derive_blockers(sources)
    paused = [b for b in blockers if b.kind is BlockerKind.THREAD_ERROR_PAUSE]
    assert [b.thread_id for b in paused] == ["older", "newer"]
