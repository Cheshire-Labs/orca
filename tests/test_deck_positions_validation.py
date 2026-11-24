"""The transit-input deck-site guard validates the ``deck_positions`` TARGET,
not merely its presence.

A transit labware moving onto a multi-site liquid handler needs a ``deck_positions``
entry that names a REAL deck site on that device. A missing entry, a typo, or the
internal gripper all leave the input with no working site of its own, so the guard
rejects each with ``TransitLabwareMissingDeckSiteError``: the thread pauses for the
operator with that message, and the action never runs. All deck sites are equal
under the derived-handoff model, so the site the arm is taught is itself a valid
target (direct delivery). A device with no derived deck sites (a deckless
``LiquidHandlerProtocol``) is exempt: there is nothing to site against, so no
entry is required.
"""

from cheshire_drivers.sims import SimLiquidHandlerWithProtocolDriver
from orca.events.event_bus import EventBus
import asyncio

import pytest

import orca.orca as orca
from orca.runtime.execution import Execution, ExecutionPhase
from orca.workflow_models.errors import TransitLabwareMissingDeckSiteError
from orca.spawn import DISPENSE
from cheshire_drivers import (
    CartesianCoordinates as C,
    DeckLayoutConfig,
    DeckResourceConfig,
    RecordingLiquidHandlerDriver,
    Teachpoint,
)
from orca.devices.devices import LiquidHandler, LiquidHandlerProtocol, Storage
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.startup import PackageSetupStoreFactory
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.resource_models.labware import LabwareTemplate
from orca.sdk.labware import PlateTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.setup_package import package_stores, write_deck_layouts, write_teachpoints
from tests.setup_package import with_teachpoints, with_deck_layouts
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler


DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)

# The arm reaches exactly this deck site; the gripper relays onward from it.
ARM_DECK_ENTRY = "lh/carrier-7-2"


def _arm(stores: PackageSetupStoreFactory, lh_point: str = ARM_DECK_ENTRY) -> Transporter:
    """``lh_point`` is site-qualified for a multi-site deck; a deckless handler
    owns one slot, so its bare device name is the teachable point."""
    return with_teachpoints(Transporter(
        "arm",
    ), write_teachpoints(stores, "arm", [
            Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint(lh_point, C(400, 200, 300, 0, 90, 180), orientation="right"),
            Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
        ]))


async def _build_single_transit(
    recorder: RecordingLiquidHandlerDriver, ran: list[bool], *,
    site: str | None, wf_name: str,
):
    """One transit plate runs a one-input action on a multi-site LH. ``site`` is the
    deck_positions target for the plate (None omits the entry entirely)."""
    stores = package_stores()
    plate = PlateTemplate(
        "plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    the_real_device_path().simulate_with("lh", recorder)
    lh = with_deck_layouts(LiquidHandler(
        "lh",
        deck_layout="default",
    ), write_deck_layouts(stores, "lh", {"default": DECK_CONFIG}))
    stacker = Storage("stacker")
    waste = Storage("waste")
    arm = _arm(stores)

    deck_positions: dict[LabwareTemplate, str] | None = {plate: site} if site is not None else None

    @orca.action(device=lh, inputs=[plate], deck_positions=deck_positions)
    async def use_plate(ctx: ActionContext) -> None:
        ran.append(True)

    @orca.method
    async def use_method(ctx: MethodContext):
        yield use_plate

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield use_method

    @orca.workflow(name=wf_name)
    def workflow(wf):
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": stacker, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="SingleTransit", workflow=workflow, topology=topology, stores=stores, event_bus=EventBus(), ops_history_store=memory_ops_store())


async def _build_deckless(
    simulator: SimLiquidHandlerWithProtocolDriver, ran: list[bool], *, wf_name: str,
):
    """One transit plate runs a one-input action on a deckless LiquidHandlerProtocol
    (no derived deck sites), with no deck_positions."""
    stores = package_stores()
    plate = PlateTemplate(
        "plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    the_real_device_path().simulate_with("lh", simulator)
    lh = LiquidHandlerProtocol("lh")
    stacker = Storage("stacker")
    waste = Storage("waste")
    arm = _arm(stores, lh_point="lh")

    @orca.action(device=lh, inputs=[plate])
    async def use_plate(ctx: ActionContext) -> None:
        ran.append(True)

    @orca.method
    async def use_method(ctx: MethodContext):
        yield use_plate

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield use_method

    @orca.workflow(name=wf_name)
    def workflow(wf):
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": stacker, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Deckless", workflow=workflow, topology=topology, stores=stores, event_bus=EventBus(), ops_history_store=memory_ops_store())


_ENDED = (
    ExecutionPhase.FAILED, ExecutionPhase.ABORTED, ExecutionPhase.COMPLETED, ExecutionPhase.PARTIAL_COMPLETED,
)


async def _run_until_it_stops(build, wf_name: str) -> tuple[SystemRuntime, Execution, ThreadSnapshot | None]:
    """Run until the execution ends or a thread pauses on an error; returns that thread, if any."""
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow(wf_name, mode=WorkflowRunMode.CHECK_RUN)
    execution = runtime._executions[record.id]
    for _ in range(200):
        if execution.phase in _ENDED:
            return runtime, execution, None
        paused = [t for t in runtime.get_execution_detail(record.id).threads if t.pause_reason == "error"]
        if paused:
            return runtime, execution, paused[0]
        await asyncio.sleep(0.1)
    return runtime, execution, None


def _refusal(execution: Execution, paused: ThreadSnapshot | None) -> str:
    """A refused deck site pauses the thread for an operator decision; the run does not end. Returns the error."""
    assert paused is not None, f"the thread must pause on the refusal; got phase={execution.phase} error={execution.error!r}"
    assert paused.pause_site == "UNHANDLED_ERROR" and paused.honoured_decisions == ("ABORT_THREAD",), paused
    assert execution.phase not in _ENDED, execution.phase
    return paused.last_error or ""


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_valid_deck_site_completes() -> None:
    """A transit plate whose deck_positions names a real free deck site runs the
    action and completes. Positive control for the validation below."""
    recorder = RecordingHandler()
    ran: list[bool] = []
    build = await _build_single_transit(recorder, ran, site="carrier-7-0", wf_name="dp_valid")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_valid")
    try:
        assert execution.phase is ExecutionPhase.COMPLETED, (
            f"a valid deck site must let the action run and complete; "
            f"got phase={execution.phase} error={execution.error!r}")
        assert ran, "the action body must run for a validly sited transit input"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_nonexistent_deck_site_fails() -> None:
    """A deck_positions target that names no site on the device (typo) is refused, and the error names the bad site."""
    recorder = RecordingHandler()
    ran: list[bool] = []
    build = await _build_single_transit(recorder, ran, site="carrier-99-9", wf_name="dp_fake")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_fake")
    try:
        err = _refusal(execution, paused)
        assert "carrier-99-9" in err, (
            f"the failure must name the bad site; got {err!r}")
        assert "use_plate" in err, (
            f"the fix is a deck_positions entry on one action's decorator, so the "
            f"failure must name that action; got {err!r}")
        assert not ran, "the action body must not run for an invalid deck site"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_device_prefixed_deck_site_fails_with_bare_hint() -> None:
    """A deck_positions value that includes the device prefix double-prefixes:
    the engine prefixes the action's device, so 'lh/carrier-7-0' resolves to
    'lh/lh/carrier-7-0' and matches nothing. The failure shows the doubled
    resolution and points at the bare-site rule."""
    recorder = RecordingHandler()
    ran: list[bool] = []
    build = await _build_single_transit(
        recorder, ran, site="lh/carrier-7-0", wf_name="dp_prefixed")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_prefixed")
    try:
        err = _refusal(execution, paused)
        assert "lh/lh/carrier-7-0" in err, (
            f"the failure must show the doubled resolution; got {err!r}")
        assert "bare" in err.lower(), (
            f"the failure must point at the bare-site rule; got {err!r}")
        assert not ran, "the action body must not run for a double-prefixed site"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_wrong_device_prefixed_deck_site_fails_without_false_drop_hint() -> None:
    """A deck_positions value prefixed with a DIFFERENT device name still fails
    with the bare-site rule, but must not claim the value carries the action's
    device prefix or tell the author to drop a prefix that is not there."""
    recorder = RecordingHandler()
    ran: list[bool] = []
    build = await _build_single_transit(
        recorder, ran, site="other_lh/carrier-7-0", wf_name="dp_wrong_prefix")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_wrong_prefix")
    try:
        err = _refusal(execution, paused)
        assert "lh/other_lh/carrier-7-0" in err, (
            f"the failure must show what the engine looked for; got {err!r}")
        assert "bare" in err.lower(), (
            f"the failure must point at the bare-site rule; got {err!r}")
        assert "already device-prefixed" not in err and "Drop the 'lh/'" not in err, (
            f"the failure must not claim the action's device prefix is present; got {err!r}")
        assert not ran, "the action body must not run for a wrong-prefixed site"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_arm_taught_site_is_a_valid_deck_site() -> None:
    """The site the arm is taught is an ordinary deck site, not a reserved
    handoff: pointing deck_positions at it is legal and the arm delivers the
    transit plate there directly, with no gripper relay in between."""
    recorder = RecordingHandler()
    ran: list[bool] = []
    site = ARM_DECK_ENTRY.split("/", 1)[1]
    build = await _build_single_transit(recorder, ran, site=site, wf_name="dp_arm_site")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_arm_site")
    try:
        assert execution.phase is ExecutionPhase.COMPLETED, (
            f"the arm-taught site is a normal working site; the execution must "
            f"complete; got phase={execution.phase} error={execution.error!r}")
        assert ran, "the action body must run for a plate sited at the arm's entry"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_gripper_is_not_a_valid_deck_site() -> None:
    """The internal gripper is a real node on the device but a transporter, not
    a DeckSite, so it cannot hold a transit input; deck_positions at it fails."""
    recorder = RecordingHandler()
    ran: list[bool] = []
    build = await _build_single_transit(recorder, ran, site="gripper", wf_name="dp_gripper")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_gripper")
    try:
        err = _refusal(execution, paused)
        assert "gripper" in err, (
            f"the failure must name the bad target; got {err!r}")
        assert not ran, "the action body must not run"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_missing_deck_positions_entry_fails() -> None:
    """No deck_positions entry at all on a multi-site deck fails with the
    missing-entry message (distinct from the invalid-target message)."""
    recorder = RecordingHandler()
    ran: list[bool] = []
    build = await _build_single_transit(recorder, ran, site=None, wf_name="dp_missing")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_missing")
    try:
        err = _refusal(execution, paused)
        assert "no deck_positions entry" in err, (
            f"the failure must report the missing entry; got {err!r}")
        assert not ran, "the action body must not run"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_transit_onto_deckless_device_needs_no_deck_positions() -> None:
    """A device with no derived deck sites (deckless LiquidHandlerProtocol) is
    exempt: a transit plate runs its action and completes with no deck_positions."""
    simulator = SimLiquidHandlerWithProtocolDriver("lh")
    ran: list[bool] = []
    build = await _build_deckless(simulator, ran, wf_name="dp_deckless")
    runtime, execution, paused = await _run_until_it_stops(build, "dp_deckless")
    try:
        assert execution.phase is ExecutionPhase.COMPLETED, (
            f"a deckless device needs no deck_positions; the action must complete; "
            f"got phase={execution.phase} error={execution.error!r}")
        assert ran, "the action body must run on a deckless device with no site"
    finally:
        await runtime.shutdown()


def test_the_refusal_names_the_action_whose_decorator_needs_the_entry() -> None:
    """The fix goes on one @orca.action, and a thread yields several of them.

    Naming the labware and the device leaves the operator reading every action in
    the file to work out which one moves that plate onto that deck.
    """
    refusal = TransitLabwareMissingDeckSiteError(
        "plate_a-bbafd1c6", "lh", "plate_a", ["lh/carrier-7-0"], "use_plate",
    )

    assert "use_plate" in str(refusal)


def test_a_site_that_matches_nothing_names_the_action_too() -> None:
    """Same fix, same question: which decorator holds the wrong value."""
    refusal = TransitLabwareMissingDeckSiteError(
        "plate_a-bbafd1c6", "lh", "plate_a", ["lh/carrier-7-0"], "use_plate",
        declared_site="carrier-99-9",
    )

    assert "use_plate" in str(refusal)
    assert "carrier-99-9" in str(refusal)


def test_the_listed_sites_are_the_values_deck_positions_takes() -> None:
    """The operator's next move is to paste a listed site into the decorator.

    Site ids carry their device and deck_positions does not, so listing the ids
    hands them the double-prefixed value this same message warns about.
    """
    refusal = TransitLabwareMissingDeckSiteError(
        "plate_a-bbafd1c6", "lh", "plate_a",
        ["lh/carrier-7-0", "lh/carrier-25-3"], "use_plate",
    )

    assert "carrier-7-0" in str(refusal)
    assert "lh/carrier-7-0" not in str(refusal)

