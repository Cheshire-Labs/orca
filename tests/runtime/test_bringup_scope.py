"""Bring-up is scoped to what a workflow declares.

The first execution under a run mode brings devices up. It used to bring up
EVERY device in the topology, which on the bench meant a Flex-only run drove
the PF400 arm into the Opentrons, because bring-up homed back then.

Bring-up asks for no motion now, so an arm whose driver offers the granular
lifecycle cannot be driven that way again, but the scoping still stands on its
own: bringing a device up takes its session and resets what it was tracking,
and a workflow that never touches the arm has no business doing that to it.

What a workflow declares is where each of its threads starts and ends, plus
the movers on a route between two declared positions. A thread body is
arbitrary Python, so anything it reaches beyond that is brought up on first
use instead (`Transporter.ensure_initialized`).
"""

from collections.abc import AsyncGenerator

import pytest

from orca.events.event_bus import EventBus
from orca.resource_models.devices import Device
from orca.resource_models.labware import PlateTemplate
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate

from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    seeded,
    wire_system_map,
)
from tests.runtime.test_lazy_init_and_resolver import (
    CountingInitDriver,
    UniversalMockDevice,
)

pytestmark = pytest.mark.asyncio


class CountingMoves:
    """Records the motion a transporter bring-up actually performs."""

    def __init__(self) -> None:
        self.initialize_calls = 0
        self.move_to_safe_calls = 0


def _spy_on_bringup(transporter, spy: CountingMoves) -> None:
    """Wrap the driver's bring-up calls so a test can see the arm move."""
    driver = transporter.live_driver
    real_initialize = driver.initialize
    real_move_to_safe = driver.move_to_safe

    async def initialize(request):
        spy.initialize_calls += 1
        return await real_initialize(request)

    async def move_to_safe(request):
        spy.move_to_safe_calls += 1
        return await real_move_to_safe(request)

    driver.initialize = initialize
    driver.move_to_safe = move_to_safe


async def _one_device_one_arm(
    plate: PlateTemplate,
) -> tuple[SystemRuntime, CountingInitDriver, CountingMoves]:
    """A shaker on a pad, an arm that can reach both, nothing else."""
    shaker_driver = CountingInitDriver("shaker1")
    shaker: Device = UniversalMockDevice("shaker1", driver=shaker_driver)
    transporter = create_test_transporter("robot1", ["pad1", "shaker1"])
    arm_spy = CountingMoves()
    _spy_on_bringup(transporter, arm_spy)

    registry = ResourceRegistry()
    registry.add_resource(shaker)
    registry.add_resource(transporter)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shaker}, pads=["pad1"])
    builder = SdkToSystemBuilder(
        name="scoped_sys", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=EventBus())
    return runtime, shaker_driver, arm_spy


def _workflow_over(plate: PlateTemplate, start: str, end: str) -> WorkflowTemplate:
    async def journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        """Declares where the labware goes; these tests never run the body."""
        return
        yield

    workflow = WorkflowTemplate("scoped_wf")
    workflow.add_thread(
        ThreadTemplate(labware_template=plate, start=start, end=end, func=journey),
        is_start=True,
    )
    return workflow


class TestBringUpIsScopedToTheWorkflow:
    async def test_a_run_that_never_moves_labware_does_not_bring_up_the_arm(self) -> None:
        """The bench failure, as a test.

        Every declared position sits on one device, so no route is needed and
        no mover is in play. Touching the arm here is what put it into the
        Opentrons, back when bring-up homed.
        """
        plate = create_test_plate_template("plate_96")
        runtime, shaker_driver, arm_spy = await _one_device_one_arm(plate)
        workflow = _workflow_over(plate, "shaker1", "shaker1")
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized(workflow)
            assert shaker_driver.init_calls == 1, (
                "the device the workflow declares must still be brought up; "
                f"observed {shaker_driver.init_calls} initialize dispatches"
            )
            assert arm_spy.initialize_calls == 0, (
                "a workflow declaring no move must not initialize the mover; "
                f"observed {arm_spy.initialize_calls} initialize dispatches"
            )
            assert arm_spy.move_to_safe_calls == 0, (
                "bring-up drove the arm for a run that never uses it; "
                f"observed {arm_spy.move_to_safe_calls} move_to_safe dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_a_run_that_moves_between_devices_does_bring_up_the_arm(self) -> None:
        """The other half: scoping must not starve a run that needs the mover.

        Start and end sit on different resources, so a route carries the
        labware and the mover on it is in play.
        """
        plate = create_test_plate_template("plate_96")
        runtime, _shaker_driver, arm_spy = await _one_device_one_arm(plate)
        workflow = _workflow_over(plate, "pad1", "shaker1")
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized(workflow)
            assert arm_spy.initialize_calls == 1, (
                "a workflow whose labware crosses devices needs the mover up; "
                f"observed {arm_spy.initialize_calls} initialize dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_a_second_workflow_widens_the_scope(self) -> None:
        """Done-ness is per scope, not per mode.

        A first workflow that declares only the shaker must not mark the
        arm's bring-up done for the next one, or the mover is left to the
        first-use fallback in every multi-workflow deployment.
        """
        plate = create_test_plate_template("plate_96")
        runtime, _shaker_driver, arm_spy = await _one_device_one_arm(plate)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized(
                    _workflow_over(plate, "shaker1", "shaker1"),
                )
                assert arm_spy.initialize_calls == 0
                await runtime.system.ensure_runtime_initialized(
                    _workflow_over(plate, "pad1", "shaker1"),
                )
            assert arm_spy.initialize_calls == 1, (
                "the second workflow declares a move, so the arm must come up; "
                f"observed {arm_spy.initialize_calls} initialize dispatches"
            )
        finally:
            await runtime.shutdown()

    async def test_no_workflow_still_brings_everything_up(self) -> None:
        """A caller with no workflow cannot be narrowed, so nothing is skipped."""
        plate = create_test_plate_template("plate_96")
        runtime, shaker_driver, arm_spy = await _one_device_one_arm(plate)
        try:
            await runtime.start()
            with seeded(WorkflowRunMode.LIVE):
                await runtime.system.ensure_runtime_initialized()
            assert shaker_driver.init_calls == 1
            assert arm_spy.initialize_calls == 1, (
                "with nothing declared the walk must stay conservative; "
                f"observed {arm_spy.initialize_calls} initialize dispatches"
            )
        finally:
            await runtime.shutdown()
