"""Regression tests for shared-action failure recovery and mid-action stop.

Three behaviors are pinned:

1. A bound shared-action device failure pauses the WHOLE action group -- the owner
   (which runs the device op) AND every joined contributor -- and records ONE incident
   (the owner's root cause). Contributors pause in sympathy, record nothing, and await
   the owner's single published outcome. Recovering ANY participant feeds the group's
   one decision: RETRY re-drives and every participant rides along; a terminal decision
   tears the group down so every participant reaches the right terminal state. (The
   spurious "Action has errored, cannot execute" record is asserted absent.)

2. ``abort_execution`` mid-action drives the action to a terminal ABORTED state
   via ``ACTION_CANCELLED`` and releases its reservation. No
   ``InvalidActionTransition`` incident is created and the action does not stay
   stuck at EXECUTING_ACTION.

3. After a mid-action abort, the SAME workflow re-runs and advances past CREATED
   (acquires its start reservation) instead of wedging on a leaked reservation.
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.devices.device_interfaces import ICentrifuge
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
)
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.actions.executable_location_action import (
    ExecutableLocationAction,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.error_policy_overrides import OverrideWithPauseError
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import (
    ActionStatus,
    FailurePolicy,
    RecoveryDecision,
)
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    wire_system_map,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
)


_TERMINAL_ACTION_STATUSES = frozenset(
    {
        ActionStatus.COMPLETED,
        ActionStatus.ERRORED,
        ActionStatus.ABORTED,
        ActionStatus.SKIPPED,
    }
)


class _FailOnceDevice(UniversalMockDevice):
    """Shake raises until ``should_fail`` is cleared. With ``override_pause`` it
    raises an ``OverrideWithPauseError`` (a coordination signal that forces
    PAUSE even under ABORT policy) instead of a plain RuntimeError."""

    def __init__(
        self,
        name: str,
        override_pause: bool = False,
        site_names: list[str] | None = None,
    ) -> None:
        super().__init__(name, site_names=site_names)
        self.should_fail = True
        self.shake_count = 0
        self._override_pause = override_pause

    async def shake(self, duration: int, speed: int) -> None:
        self.shake_count += 1
        if self.should_fail:
            if self._override_pause:
                raise OverrideWithPauseError("device under external control (test)")
            raise RuntimeError("Simulated shake failure")
        await super().shake(duration, speed)


class _FailOnceCentrifuge(UniversalMockDevice):
    """Centrifuge whose spin raises until ``should_fail`` is cleared. Physically a
    centrifuge balances several plates in one spin, so a multi-input shared action on it
    is a realistic co-residency -- the reason these tests target a centrifuge over a
    single-plate shaker. (The sim caps no device; this is a physically honest config
    choice, not a modeled capacity.)"""

    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.should_fail = True
        self.spin_count = 0

    async def centrifuge(self, g: int, duration: int) -> None:
        self.spin_count += 1
        if self.should_fail:
            raise RuntimeError("Simulated centrifuge failure")
        await super().centrifuge(g, duration)


class _HangingDevice(UniversalMockDevice):
    """Shake blocks until ``release`` is set, exposing a long-running action.

    ``in_shake`` lets a test wait until the thread is firmly in
    EXECUTING_ACTION before it calls ``abort_execution``.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.in_shake = asyncio.Event()
        self.release = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.in_shake.set()
        await self.release.wait()
        await super().shake(duration, speed)


async def _wait_for_shared_rendezvous(
    runtime: SystemRuntime,
    execution_id: str,
    *,
    contributors: int = 1,
    timeout: float = 10.0,
) -> list[ThreadSnapshot]:
    """Wait until a bound shared-action failure has paused the whole action group: the
    owner AND every joined contributor are PAUSED, so the operator sees the entire
    action stuck rather than a single thread. An action is one device; when it fails
    every piece of labware converged on it is physically stuck, so every participant
    pauses. Anchoring a settled-state assertion on this positive signal keeps it off the
    wall clock. (A pre-binding resolution failure places no labware at the device, so
    only the owner pauses there and ``wait_for_paused_threads`` is used instead.) Returns
    the paused participants."""
    expected = 1 + contributors

    def _settled() -> bool:
        return len(runtime.get_paused_threads(execution_id)) == expected

    await wait_until(_settled, timeout=timeout)
    return runtime.get_paused_threads(execution_id)


# ---------------------------------------------------------------------------
# Test 1: co-thread shared-action failure -> single incident, clean co-exit
# ---------------------------------------------------------------------------


async def _build_join_system(
    failure_policy: FailurePolicy = FailurePolicy.PAUSE,
    override_pause: bool = False,
) -> tuple[SystemRuntime, WorkflowTemplate, _FailOnceDevice]:
    """One owner thread runs a multi-input shake; a contributor joins it.

    This is the tip-rack-lane-joins-shake shape: the shake action declares
    ``[plate_main, plate_child]`` as inputs, and ``child_thread`` joins via
    ``orca.join()`` so both threads converge on the same shared action.
    """
    # Two-input shared action: single occupancy requires one working site per
    # simultaneously-present labware.
    device = _FailOnceDevice(
        "shaker1", override_pause=override_pause, site_names=["site-1", "site-2"]
    )
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2"])
    plate_main = create_test_plate_template("plate_main")
    plate_child = create_test_plate_template("plate_child")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

    @orca.action(
        device=pool, inputs=[plate_main, plate_child], failure_policy=failure_policy
    )
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def parent_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    pad1 = system_map.get_location("pad1")
    child_loc = system_map.get_location("pad2")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def main_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield parent_method

    @orca.thread(labware=plate_child, start=child_loc, end=child_loc)
    async def child_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join()

    @orca.workflow(name="join_fail_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(main_thread)
        wf.thread(child_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate_main, plate_child],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, device


async def _build_spin_group_system(
    failure_policy: FailurePolicy = FailurePolicy.ABORT,
) -> tuple[SystemRuntime, WorkflowTemplate, _FailOnceCentrifuge]:
    """One owner plus two contributors converge on a multi-plate centrifuge for a single
    shared spin action. A centrifuge holds every converged plate at once (balanced spin),
    so a three-input action on it is a physically honest co-residency -- which a
    single-plate shaker could not do on real hardware. Used to pin that a group abort
    tears the WHOLE group down (owner + both contributors) with no follower left stranded
    on the outcome."""
    # Three balanced plates spin at once: one working site per input.
    device = _FailOnceCentrifuge(
        "centrifuge1", site_names=["site-1", "site-2", "site-3"]
    )
    transporter = create_test_transporter(
        "robot1", ["centrifuge1", "pad1", "pad2", "pad3"]
    )
    plate_main = create_test_plate_template("plate_main")
    plate_c1 = create_test_plate_template("plate_c1")
    plate_c2 = create_test_plate_template("plate_c2")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("centrifuge1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"centrifuge1": device}, pads=["pad1", "pad2", "pad3"]
    )

    @orca.action(
        device=pool,
        inputs=[plate_main, plate_c1, plate_c2],
        failure_policy=failure_policy,
    )
    async def spin_action(ctx: ActionContext) -> None:
        await ctx.device(ICentrifuge).centrifuge(g=1000, duration=1)

    @orca.method
    async def spin_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        yield spin_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")
    pad3 = system_map.get_location("pad3")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def owner_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield spin_method

    @orca.thread(labware=plate_c1, start=pad2, end=pad2)
    async def contributor_one(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join()

    @orca.thread(labware=plate_c2, start=pad3, end=pad3)
    async def contributor_two(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join()

    @orca.workflow(name="spin_group_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(owner_thread)
        wf.thread(contributor_one)
        wf.thread(contributor_two)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate_main, plate_c1, plate_c2],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, device


async def test_group_abort_terminates_every_participant_without_strand() -> None:
    """A three-thread shared action -- owner plus two contributors converged on a multi-
    plate centrifuge -- fails under ABORT policy. The owner publishes ABORT_THREAD to the
    action group and both contributors fan out to that one outcome, landing ABORTED rather
    than following the torn-down action forever (the strand the group outcome prevents).
    The owner itself stays EXECUTING_ACTION as the execution terminates -- a pre-existing
    status-reporting gap, not a strand and not this test's subject."""
    runtime, workflow, _ = await _build_spin_group_system(
        failure_policy=FailurePolicy.ABORT
    )
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    def _contributor_statuses() -> list[str]:
        return [
            t.status
            for t in runtime.list_threads(record.id)
            if t.name.startswith("plate_c")
        ]

    try:
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status != ExecutionState.RUNNING

        # Both contributors must be released to a terminal ABORTED via the owner's
        # published group outcome -- neither left following the torn-down action.
        await wait_until(
            lambda: _contributor_statuses().count("ABORTED") == 2,
            timeout=10.0,
        )
        assert _contributor_statuses().count("ABORTED") == 2, (
            "both contributors must fan out to ABORTED (no strand); got "
            f"{[(t.name, t.status) for t in runtime.list_threads(record.id)]}"
        )
    finally:
        await runtime.shutdown()


class TestCoThreadSharedActionFailurePausesEveryParticipant:
    """A bound shared-action device failure pauses the whole action group -- owner AND
    every joined contributor -- but records ONE incident (the owner's root cause);
    contributors pause in sympathy and record nothing. Recovering ANY participant feeds
    the group's single decision and drives every participant to the matching terminal
    state -- RETRY re-drives and all complete, a terminal decision tears it down."""

    async def test_every_participant_pauses_with_one_incident(self) -> None:
        runtime, workflow, device = await _build_join_system()
        await runtime.start()
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM
        )
        try:
            await _wait_for_shared_rendezvous(runtime, record.id)

            # ONE incident: the owner's root shake failure. The contributor pauses in
            # sympathy and records nothing. No spurious "cannot execute".
            incidents = await runtime.incidents.list(execution_id=record.id)
            assert len(incidents) == 1, (
                "Expected one incident on the owner only; got "
                f"{[(i.category, i.message) for i in incidents]}"
            )
            messages = " ".join(i.message for i in incidents)
            assert "Simulated shake failure" in messages
            assert "shake_action" in messages
            assert "cannot execute" not in messages

            # The shake body ran exactly once -- the contributor never re-invoked it.
            assert device.shake_count == 1

            # The whole action group PAUSES: owner + contributor.
            paused = runtime.get_paused_threads(record.id)
            assert len(paused) == 2, (
                "A bound shared-action failure pauses every participant; got "
                f"{sorted(t.status for t in runtime.list_threads(record.id))}."
            )
            assert all(p.last_error is not None for p in paused)
        finally:
            for paused in runtime.get_paused_threads(record.id):
                runtime.recover_thread(
                    record.id, paused.id, RecoveryDecision.ABORT_THREAD
                )
            try:
                await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
            except Exception:
                pass
            await runtime.shutdown()

    async def test_shared_failure_retry_completes(self) -> None:
        """Recovering the OWNER with RETRY re-drives the shared action (now that
        the device is fixed); the contributor rides along and the workflow
        COMPLETES. The shake re-drives exactly once (2 total)."""
        runtime, workflow, device = await _build_join_system()
        await runtime.start()
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM
        )
        try:
            await _wait_for_shared_rendezvous(runtime, record.id)
            assert len(runtime.get_paused_threads(record.id)) == 2
            assert device.shake_count == 1

            device.should_fail = False
            owner = runtime.get_paused_threads(record.id)[0]
            runtime.recover_thread(record.id, owner.id, RecoveryDecision.RETRY)

            final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED, (
                f"Retry of the shared failure should complete the workflow; "
                f"got {final.status}."
            )
            # The shake re-drove exactly once on retry -- not duplicated.
            assert device.shake_count == 2
        finally:
            await runtime.shutdown()

    async def test_abort_policy_terminates_without_strand(self) -> None:
        """Under ABORT policy a shared-action failure has no operator loop: the
        owner's ABORT branch publishes RECOVERED(ABORT_THREAD) on the slot and
        re-raises. The contributor fans out to that outcome and lands ABORTED (no
        strand, no crash-class incident); the execution terminates."""
        runtime, workflow, device = await _build_join_system(
            failure_policy=FailurePolicy.ABORT
        )
        await runtime.start()
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM
        )
        try:
            final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)

            # The execution terminates (no hang) and the shake ran once.
            assert final.status != ExecutionState.RUNNING
            assert device.shake_count == 1

            # Contributor lands ABORTED just after the execution terminates on the
            # owner's raise; wait for it. (Owner stays EXECUTING_ACTION: pre-existing gap.)
            await wait_until(
                lambda: [t.status for t in runtime.list_threads(record.id)].count(
                    "ABORTED"
                )
                == 1,
                timeout=10.0,
            )
            statuses = [t.status for t in runtime.list_threads(record.id)]
            assert statuses.count("ABORTED") == 1, (
                f"The contributor should fan out to ABORTED; got {statuses}."
            )

            # No incident mentions the torn-down-action crash classes.
            blobs = " ".join(i.message for i in await runtime.incidents.list())
            assert "cannot determine failure policy" not in blobs
            assert "AssertionError" not in blobs
        finally:
            await runtime.shutdown()

    async def test_shared_failure_repeated_retry_completes(self) -> None:
        """Back-to-back shared failures: RETRY the owner (fails again), RETRY it
        again after the fix, and the workflow COMPLETES. The shake drives exactly
        once per cycle (3 total), proving the re-arm re-applies cleanly."""
        runtime, workflow, device = await _build_join_system()
        await runtime.start()
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM
        )

        async def _wait_for_shake(n: int) -> None:
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                if device.shake_count >= n and len(
                    runtime.get_paused_threads(record.id)
                ) == 2:
                    return
                await asyncio.sleep(0.05)
            raise TimeoutError(f"shake_count never reached {n} with the group paused")

        try:
            # Round 1: first failure, the action group pauses.
            await _wait_for_shake(1)
            owner = runtime.get_paused_threads(record.id)[0]
            runtime.recover_thread(record.id, owner.id, RecoveryDecision.RETRY)

            # Round 2: device still broken -> re-drives, fails again, re-pauses.
            await _wait_for_shake(2)
            device.should_fail = False
            owner = runtime.get_paused_threads(record.id)[0]
            runtime.recover_thread(record.id, owner.id, RecoveryDecision.RETRY)

            # Round 3: fixed -> completes.
            final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
            assert final.status == ExecutionState.COMPLETED
            assert device.shake_count == 3
        finally:
            await runtime.shutdown()

    async def test_override_pause_on_abort_policy_pauses_every_participant(self) -> None:
        """An OverrideWithPauseError (e.g. gateway control) pauses even an ABORT-policy
        action. The device is externally held, so the whole action group parks: owner +
        contributor. Overrides are coordination signals, not failures, so no incident
        is recorded."""
        runtime, workflow, device = await _build_join_system(
            failure_policy=FailurePolicy.ABORT, override_pause=True
        )
        await runtime.start()
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM
        )
        try:
            await _wait_for_shared_rendezvous(runtime, record.id)

            paused = runtime.get_paused_threads(record.id)
            assert len(paused) == 2, (
                "An override-pause on an ABORT-policy shared action pauses the whole "
                "group; got "
                f"{sorted(t.status for t in runtime.list_threads(record.id))}."
            )
            # An override is a coordination signal, not a failure -- no incident.
            assert await runtime.incidents.list(execution_id=record.id) == []
        finally:
            for paused in runtime.get_paused_threads(record.id):
                runtime.recover_thread(
                    record.id, paused.id, RecoveryDecision.ABORT_THREAD
                )
            try:
                await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
            except Exception:
                pass
            await runtime.shutdown()


# ---------------------------------------------------------------------------
# Tests 2 + 3: stop mid-action is clean + terminal, and does not wedge re-runs
# ---------------------------------------------------------------------------


async def _build_hanging_system() -> tuple[
    SystemRuntime, WorkflowTemplate, _HangingDevice, LocationReservationManager, ISystem
]:
    device = _HangingDevice("shaker1")
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
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    pad = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad, end=pad)
    async def plate_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method

    workflow = WorkflowTemplate("hang_wf")
    workflow.add_thread(plate_thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    res_mgr = builder._thread_reservation_coordinator._reservation_manager
    runtime = SystemRuntime(system, event_bus=event_bus)
    return runtime, workflow, device, res_mgr, system


def _capture_executing_action(system: ISystem) -> ExecutableLocationAction:
    """Return the single action currently bound to a running thread.

    Captured BEFORE ``abort_execution`` clears ``assigned_action`` so the test
    can read the action's terminal status after the abort.
    """
    for thread in system.executing_threads:
        if thread.assigned_action is not None:
            return thread.assigned_action
    raise AssertionError("No thread had an assigned action to capture")


class TestStopMidActionIsCleanAndTerminal:
    """``abort_execution`` mid-action drives the action terminal (ABORTED) and
    releases its reservation without an ``InvalidActionTransition`` incident."""

    async def test_stop_mid_action_terminal_and_releases_reservation(self) -> None:
        runtime, workflow, device, res_mgr, system = await _build_hanging_system()
        await runtime.start()
        try:
            record = await runtime.submit_workflow(
                workflow.name, mode=WorkflowRunMode.PURE_SIM
            )
            # Block until the action is firmly executing on the device.
            await asyncio.wait_for(device.in_shake.wait(), timeout=10.0)

            action = _capture_executing_action(system)
            assert action.status == ActionStatus.EXECUTING_ACTION
            assert "shaker1" in res_mgr.reservations

            await runtime.abort_execution(record.id)
            final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

            assert final.status == ExecutionState.ABORTED

            # The action reached a terminal state (ABORTED) rather than staying
            # stuck at EXECUTING_ACTION.
            assert action.status in _TERMINAL_ACTION_STATUSES, (
                f"Action should be terminal after stop; got {action.status.name}"
            )
            assert action.status == ActionStatus.ABORTED

            # The cancel path released the device reservation, so the location
            # is free for the next run.
            assert "shaker1" not in res_mgr.reservations

            # No InvalidActionTransition (or any) incident was recorded -- an
            # operator stop is not a failure.
            incidents = await runtime.incidents.list()
            assert incidents == [], (
                f"Stop must not record an incident; got "
                f"{[(i.category, i.message) for i in incidents]}"
            )
        finally:
            device.release.set()
            await runtime.shutdown()


class TestStopPreservesLabware:
    """persist-until-clear boundary: an abort releases the run's reservations
    but must NOT remove its labware. The plate is physically on the deck until
    an explicit operator clear, so it stays in the ledger after a stop."""

    async def test_abort_releases_reservation_but_keeps_labware(self) -> None:
        runtime, workflow, device, res_mgr, system = await _build_hanging_system()
        await runtime.start()
        try:
            record = await runtime.submit_workflow(
                workflow.name, mode=WorkflowRunMode.PURE_SIM
            )
            await asyncio.wait_for(device.in_shake.wait(), timeout=10.0)

            assert "shaker1" in res_mgr.reservations
            tracked_before = {
                lw.id for lw in system.labware_location_service.get_all()
            }
            assert tracked_before, "plate must be tracked in the ledger while running"

            await runtime.abort_execution(record.id)
            await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)

            # Coordination is transient: a stopped thread must release its slot.
            assert "shaker1" not in res_mgr.reservations

            # persist-until-clear: the abort must not drop the plate from the
            # ledger -- it is still physically on the deck until cleared.
            tracked_after = {
                lw.id for lw in system.labware_location_service.get_all()
            }
            assert tracked_after == tracked_before, (
                "abort must not remove labware from the ledger; "
                f"before={tracked_before} after={tracked_after}"
            )
        finally:
            device.release.set()
            await runtime.shutdown()


class TestStopDoesNotWedgeNextRun:
    """After a mid-action stop, the next run of the same workflow must acquire
    its start reservation and complete, instead of wedging on the leaked
    reservation the stopped run would otherwise have left behind."""

    async def test_next_run_completes_after_stop(self) -> None:
        runtime, workflow, device, res_mgr, system = await _build_hanging_system()
        await runtime.start()
        try:
            record1 = await runtime.submit_workflow(
                workflow.name, mode=WorkflowRunMode.PURE_SIM
            )
            await asyncio.wait_for(device.in_shake.wait(), timeout=10.0)
            await runtime.abort_execution(record1.id)
            await asyncio.wait_for(runtime.wait(record1.id), timeout=10.0)

            # The stopped run released its device reservation. Pre-fix the
            # leaked reservation would block the next run forever.
            assert "shaker1" not in res_mgr.reservations, (
                "The stopped run leaked its shaker1 reservation; the next run "
                "would wedge waiting for it."
            )

            # Run 1's plate persists on the shaker site (persist-until-clear);
            # the operator clear frees it so run 2 tests only the reservation.
            await runtime.labware.clear_all_labware()

            # Second run: let the device complete normally this time. It must
            # acquire the (now-free) reservation and reach COMPLETED rather
            # than hang.
            device.release.set()
            record2 = await runtime.submit_workflow(
                workflow.name, mode=WorkflowRunMode.PURE_SIM
            )
            final2 = await asyncio.wait_for(runtime.wait(record2.id), timeout=15.0)
            assert final2.status == ExecutionState.COMPLETED, (
                f"Second run should complete after the stop; got {final2.status}. "
                "A hang here means the first run leaked its start reservation."
            )
        finally:
            device.release.set()
            await runtime.shutdown()
