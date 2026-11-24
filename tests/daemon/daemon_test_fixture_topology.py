"""Topology and workflow builders for the daemon tests.

`tests/daemon/fixture_package.py` writes a package folder whose `topology.py`
and `workflows/<name>.py` hand over to these. The shake action sleeps briefly
so tests that assert on mid-execution state have a deterministic window.
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.sdk.build import Topology
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.sdk.workflow import MethodTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
)


def build_topology() -> Topology:
    """One shaker, one transporter, one pad it reaches and two shelves it does not. Fresh objects per call.

    No orca-client serves the daemon's own runtime, so an operator's labware verbs use the shelves:
    a location a transporter reaches is mirrored into that transporter's world.
    """
    shaker = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    from orca.resource_models.plate_pad import PlatePad

    return Topology(
        locations={
            "shaker1": shaker, "pad1": PlatePad("pad1"),
            "shelf1": PlatePad("shelf1", supports_deadlock_resolution=False),
            "shelf2": PlatePad("shelf2", supports_deadlock_resolution=False),
        },
        transporters=[transporter],
        pools=[ResourcePool("shaker1", [shaker])],
    )


def build_workflow(topology: Topology) -> WorkflowTemplate:
    """A workflow that shakes a plate once and returns it to the pad."""
    plate = create_test_plate_template("plate_96")
    shaker_pool = topology.pool("shaker1")

    @orca.action(device=shaker_pool, inputs=[plate])
    async def shake_action(ctx: object) -> None:
        await ctx.device().shake(duration=1, speed=500)
        await asyncio.sleep(2.5)

    @orca.method
    async def shake_method(ctx: object) -> AsyncGenerator[object, None]:
        yield shake_action

    @orca.thread(labware=plate, start="pad1", end="pad1")
    async def plate_journey(ctx: object) -> AsyncGenerator[object, None]:
        yield shake_method

    @orca.workflow(name="simple_workflow")
    def simple_workflow(wf: object) -> None:
        wf.start(plate_journey)

    return simple_workflow


def build_workflow_slow(topology: Topology) -> WorkflowTemplate:
    """Same shape as ``build_workflow`` but the action holds the thread
    mid-execution far longer than any CLI round-trip chain, so tests that
    must act on a LIVE execution (thread spawn) cannot lose a wall-clock
    race under parallel-suite load."""
    plate = create_test_plate_template("plate_96_slow")
    shaker_pool = topology.pool("shaker1")

    @orca.action(device=shaker_pool, inputs=[plate])
    async def slow_shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)
        await asyncio.sleep(60.0)

    @orca.method
    async def slow_shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield slow_shake_action

    @orca.thread(labware=plate, start="pad1", end="pad1")
    async def slow_plate_journey(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield slow_shake_method

    @orca.workflow(name="slow_window_workflow")
    def slow_window_workflow(wf: WorkflowContext) -> None:
        wf.start(slow_plate_journey)

    return slow_window_workflow


def build_workflow_waiting_for_an_event(topology: Topology) -> WorkflowTemplate:
    """A plate that shakes, then waits for an event nothing emits, with no timeout of its own."""
    plate = create_test_plate_template("plate_96")

    @orca.action(device=topology.pool("shaker1"), inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    @orca.thread(labware=plate, start="pad1", end="pad1")
    async def waiting_plate(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield shake_method
        yield orca.on("never_emitted")

    @orca.workflow(name="waits_for_an_event")
    def waits_for_an_event(wf: WorkflowContext) -> None:
        wf.start(waiting_plate)

    return waits_for_an_event
