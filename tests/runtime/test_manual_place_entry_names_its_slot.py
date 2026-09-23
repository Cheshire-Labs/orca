"""An entry thread may name the slot its labware will be placed at.

`start=("pad1", MANUAL_PLACE)` and `start="pad1"` describe the same thread.
Both dispatch to `ManualPlaceSpawn`, and both park at `AWAITING_MANUAL_PLACE`
under LIVE until a register fires. The build-time refusal used to reject the
spelling that says so out loud while admitting the one that leaves it
implicit.

Two concurrent LIVE submissions mint two entry threads waiting on one slot.
That used to bind a single operator place to both, and a submit-time refusal
stood in the way of it. The wait now asks the ledger whether ITS OWN labware
arrived, and a register takes the longest-waiting expectation, so the two
threads take two different plates in the order they started waiting. The last
test here pins that.
"""

import asyncio

import pytest

from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.runtime.system_runtime import SystemRuntime

from tests.runtime.manual_place_fixtures import (
    _build_manual_place_system,
    _live_connection_source,
)


async def _await_parked_at_manual_place(
    runtime: SystemRuntime, execution_id: str, *, timeout: float = 15.0,
) -> tuple[str, str]:
    """Poll until a thread parks for a manual place; return (labware_id, waiting_for)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for thread in runtime.list_threads(execution_id):
            if (
                thread.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.name
                and thread.labware_id is not None
            ):
                return thread.labware_id, thread.waiting_for or ""
        await asyncio.sleep(0.05)
    states = [f"{t.name}[{t.status}]" for t in runtime.list_threads(execution_id)]
    raise AssertionError(f"no thread parked for a manual place; threads: {states}")


async def _await_two_parked(
    runtime: SystemRuntime, execution_id: str, *, timeout: float = 15.0,
) -> set[str | None]:
    """Poll until two threads are parked for a manual place; return their labware ids."""
    deadline = asyncio.get_running_loop().time() + timeout
    awaiting: set[str | None] = set()
    while asyncio.get_running_loop().time() < deadline:
        awaiting = {
            t.labware_id for t in runtime.list_threads(execution_id)
            if t.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.name
        }
        if len(awaiting) >= 2:
            return awaiting
        await asyncio.sleep(0.05)
    return awaiting


async def _explicit_manual_place_runtime(workflow_name: str) -> SystemRuntime:
    system = await _build_manual_place_system(
        workflow_name, explicit_manual_place=True,
    )
    runtime = SystemRuntime(
        system,
        labware_store=InMemoryLabwareStore(),
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    return runtime


@pytest.mark.timeout(30)
async def test_a_workflow_whose_entry_names_a_manual_place_slot_builds() -> None:
    """The build-time refusal is what blocked a source-plate thread whose
    entry is an operator placement; naming the slot must not be the thing
    that makes the workflow unbuildable."""
    system = await _build_manual_place_system(
        "wf_explicit_builds", explicit_manual_place=True,
    )
    template = system.get_workflow_template("wf_explicit_builds")
    entries = template.entry_thread_templates
    assert [t.start_dispense for t in entries] == [False]
    assert [t.start_reuse_existing for t in entries] == [False]
    assert [t.start_position_id for t in entries] == ["pad1"]


@pytest.mark.timeout(30)
async def test_an_explicit_manual_place_entry_parks_at_the_slot_it_names() -> None:
    """Same runtime state the bare-string form reaches: parked at
    AWAITING_MANUAL_PLACE, telling the operator which slot to fill."""
    runtime = await _explicit_manual_place_runtime("wf_explicit_parks")
    try:
        submission = await runtime.submit_workflow(
            "wf_explicit_parks", mode=WorkflowRunMode.LIVE,
        )
        _labware_id, waiting_for = await _await_parked_at_manual_place(
            runtime, submission.id,
        )
        assert "pad1" in waiting_for
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_registering_at_the_named_slot_binds_the_awaited_labware() -> None:
    """`labware_register` releases the explicit-form park by adopting the
    instance the thread already holds, exactly as it does for bare-string."""
    runtime = await _explicit_manual_place_runtime("wf_explicit_register")
    try:
        submission = await runtime.submit_workflow(
            "wf_explicit_register", mode=WorkflowRunMode.LIVE,
        )
        awaited_id, _waiting_for = await _await_parked_at_manual_place(
            runtime, submission.id,
        )

        snap = await runtime.labware.register(
            "plate_wf_explicit_register", location="pad1", confirm=True,
        )

        assert snap.id == awaited_id
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_two_live_submissions_wait_on_one_slot_for_different_plates() -> None:
    """Both submissions are admitted, and the first place goes to the first waiter.

    Each entry thread holds its own labware identity and asks the ledger about
    that one, so neither can bind a plate the other is waiting for.
    """
    runtime = await _explicit_manual_place_runtime("wf_two_live")
    try:
        first = await runtime.submit_workflow(
            "wf_two_live", mode=WorkflowRunMode.LIVE,
        )
        first_awaited, _waiting = await _await_parked_at_manual_place(
            runtime, first.id,
        )

        second = await runtime.submit_workflow(
            "wf_two_live", mode=WorkflowRunMode.LIVE,
        )
        second_awaited, _waiting = await _await_parked_at_manual_place(
            runtime, second.id,
        )

        assert first_awaited != second_awaited

        placed = await runtime.labware.register(
            "plate_wf_two_live", location="pad1", confirm=True,
        )
        assert placed.id == first_awaited

        # The half that matters, and the half a slot-reading wait fails. The
        # register put a plate on pad1. A wait that read the slot would see it
        # standing there and take it, so BOTH threads would leave
        # AWAITING_MANUAL_PLACE on one plate. Asking the ledger about its own
        # labware, the second thread stays put.
        #
        # Held over six of that wait's half-second ticks, so the regression has
        # every chance to happen rather than being checked before it could.
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            assert [
                t for t in runtime.list_threads(second.id)
                if t.status == LabwareThreadStatus.AWAITING_MANUAL_PLACE.name
                and t.labware_id == second_awaited
            ], (
                f"the second submission's thread took the plate placed for the "
                f"first: {[(t.name, t.status, t.labware_id) for t in runtime.list_threads(second.id)]}"
            )
            await asyncio.sleep(0.05)
    finally:
        await runtime.shutdown()


@pytest.mark.timeout(30)
async def test_a_live_join_lands_in_the_execution_it_joined() -> None:
    """The path the removed refusal made unreachable.

    The refusal ran BEFORE the JOIN_EXISTING branch, so a join of a workflow
    whose entry plate is hand placed was never evaluated, and
    SubmissionBatching.BATCHABLE could not be exercised under LIVE anywhere.
    Both submissions must land in ONE execution, each waiting on its own plate.
    """
    runtime = await _explicit_manual_place_runtime("wf_live_join")
    template = runtime.system.get_workflow_template("wf_live_join")
    try:
        first = await runtime.submit(
            template,
            groups=[LabwareGroup(
                id="g1",
                members=(LabwareGroupMember(thread_template_name="plate_thread"),),
            )],
            mode=WorkflowRunMode.LIVE,
        )
        first_awaited, _waiting = await _await_parked_at_manual_place(
            runtime, first.execution_id,
        )

        second = await runtime.submit(
            template,
            groups=[LabwareGroup(
                id="g2",
                members=(LabwareGroupMember(thread_template_name="plate_thread"),),
            )],
            batch_mode=BatchMode.JOIN_EXISTING,
            mode=WorkflowRunMode.LIVE,
        )

        assert second.execution_id == first.execution_id, (
            "a LIVE join of a hand-placed entry must join the live execution, "
            "not open its own"
        )
        awaiting = await _await_two_parked(runtime, first.execution_id)
        assert len(awaiting) == 2 and first_awaited in awaiting, (
            f"both groups must wait on their own plate; awaiting {awaiting}"
        )
    finally:
        await runtime.shutdown()
