"""A thread that finished its last action on a device must give the device up.

Bench 2026-09-02: the owner of a one-action shared method finished, left the
liquid handler, and kept the device mutex. The next group's plate polled for it
for over an hour and the run died.

The drain-gated release is armed in one place a running thread reaches: the
branch taken when the thread asks its method for another action and is told
there are none. That branch is never taken, because the action-completion
handler peeks the lane and sets ``completed`` first, so the thread's loop exits
on its own condition. The only other arming site runs after the move to the end
location, and here the successor is standing on that location.
"""

import asyncio

import pytest

import orca.orca as orca
from cheshire_drivers import DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers import CartesianCoordinates as C
from orca.devices.devices import LiquidHandler
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import run_to_quiescence, wait_for_runtime_condition


_DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)
_RESERVOIR_SITE = "lh/carrier-25-0"
_SAMPLE_SITE = "lh/carrier-7-0"
# Long enough that the next plate is standing on the shared pad before the
# first plate asks for it back; without it the two race.
_ACTION_SECONDS = 2.0


async def _build():
    """One pad in and out, one deck-resident reagent, one action per plate.

    ``pad`` is every sample's start AND end, so the next plate stands on the
    exit of the plate before it. ``park`` gives the deadlock resolver somewhere
    to move a plate to, which is what the bench had.
    """
    stores = InMemoryRuntimeStoreFactory()
    sample = PlateTemplate("sample", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate("reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    lh = LiquidHandler(
        "lh",
        deck_layout_store=stores.deck_layouts("lh", seed={"default": _DECK_CONFIG}),
        deck_layout="default",
    )
    pad = PlatePad("pad")
    park = PlatePad("park")
    arm = Transporter(
        "arm",
        teachpoint_store=stores.teachpoints("arm", seed=[
            Teachpoint("pad", C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("park", C(200, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint(_SAMPLE_SITE, C(400, 200, 300, 0, 90, 180), orientation="right"),
        ]),
    )

    @orca.action(device=lh, inputs=[sample, reservoir], deck_positions={sample: "carrier-7-0"})
    async def add_reagent(ctx: ActionContext) -> None:
        ctx.labware("reservoir")
        await asyncio.sleep(_ACTION_SECONDS)

    @orca.method
    async def add(ctx: MethodContext):
        yield add_reagent

    @orca.thread(labware=sample, start="pad", end="pad")
    async def plate_journey(ctx: ThreadContext):
        yield add

    @orca.thread(
        labware=reservoir,
        start=(_RESERVOIR_SITE, REUSE_EXISTING),
        end=(_RESERVOIR_SITE, LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add])

    @orca.workflow(name="one_pad_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"lh": lh, "pad": pad, "park": park},
        transporters=[arm],
    )
    return await orca.build_system(
        name="One Pad", workflow=workflow, topology=topology, stores=stores)


async def _submit_two_groups(build, runtime: SystemRuntime):
    template = build.system.get_workflow_template("one_pad_wf")
    groups = [
        LabwareGroup(
            id=f"grp-{i}",
            members=(LabwareGroupMember(thread_template_name="plate_journey"),),
        )
        for i in range(2)
    ]
    return await runtime.submit(template, groups=groups, mode=WorkflowRunMode.PURE_SIM)


def _finished_sample(runtime: SystemRuntime, execution_id: str) -> str | None:
    """Thread id of a sample whose one and only method is done."""
    for thread in runtime.list_threads(execution_id):
        if thread.name.startswith("sample-") and thread.completed_method_count >= 1:
            return thread.id
    return None


def _successor_is_on_the_pad(
    runtime: SystemRuntime, execution_id: str, owner: str | None
) -> bool:
    """Is the other sample standing on the pad the owner has to come back to?"""
    return any(
        thread.name.startswith("sample-")
        and thread.id != owner
        and thread.current_location == "pad"
        for thread in runtime.list_threads(execution_id)
    )


def _device_hold(runtime: SystemRuntime, execution_id: str) -> LocationReservation | None:
    """The live hold on the liquid handler. Reached through internals because
    no public surface reports whether a hold is armed to drain."""
    workflow = runtime._get_execution(execution_id).executing_workflow
    assert workflow is not None
    coordinator = workflow._thread_reservation_coordinator
    assert isinstance(coordinator, ThreadReservationCoordinator)
    return coordinator._reservation_manager.get_reservation_at("lh")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_finished_owner_arms_the_device_release() -> None:
    """A hold kept past the owner's last action has to be armed to drain, so the
    successor's next ask takes it over. Unarmed, nothing frees it."""
    build = await _build()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        submission = await _submit_two_groups(build, runtime)
        execution_id = submission.execution_id

        await wait_for_runtime_condition(
            runtime,
            lambda: _finished_sample(runtime, execution_id) is not None,
            timeout=60.0,
        )
        owner = _finished_sample(runtime, execution_id)

        # The successor has to be standing on the shared pad, or the owner
        # simply walks to its end location and the hold is never tested.
        assert _successor_is_on_the_pad(runtime, execution_id, owner), (
            "setup did not reach the contended state: the next plate is not on "
            "the pad, so this run proves nothing"
        )

        hold = _device_hold(runtime, execution_id)
        if hold is not None and hold.thread_id == owner:
            assert hold.pending_drain_check is not None, (
                "the finished owner still holds lh with no drain check; nothing "
                "will release it"
            )
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_both_groups_finish_when_the_exit_pad_is_the_next_entry() -> None:
    """End to end: group 2 cannot enter until group 1 gives up the device, and
    group 1 cannot leave until group 2 vacates the pad. Only the device release
    breaks that cycle; parking a plate cannot."""
    build = await _build()
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    submission = await _submit_two_groups(build, runtime)
    statuses = await run_to_quiescence(runtime, submission.execution_id, timeout=150.0)
    await runtime.shutdown(confirm=True)

    assert statuses, "no threads reported"
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"both sample plates and the resident reagent must complete; got {statuses}"
    )
