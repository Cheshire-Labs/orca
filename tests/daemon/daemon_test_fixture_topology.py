"""Topology + workflow factories for the daemon mount/load route tests.

Mirrors the deployment-package shape the daemon now consumes:

- ``build_topology(stores)`` returns a ``Topology`` (the physical lab).
- ``build_workflow(topology)`` returns a ``WorkflowTemplate`` registered
  against that topology.

The daemon mounts the topology (``POST /mount-topology``) to build an
empty ``SystemRuntime``, then registers the workflow separately
(``POST /workflows``). The shake action sleeps briefly so tests that
assert on mid-execution state have a deterministic window.
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.sdk.workflow import MethodTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
)


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    """One shaker + one transporter + one pad. Fresh objects per call."""
    del stores
    shaker = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    from orca.resource_models.plate_pad import PlatePad

    return Topology(
        locations={"shaker1": shaker, "pad1": PlatePad("pad1")},
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
