"""A cooperative stop must still reach a contributor following a peer's action.

A contributor error-pauses by marking itself and waiting on the group, never in
``_pause_for_error``, so it reads PAUSED with an error while watching a channel
the recovery verbs do not write to. It stays that way across an operator RETRY,
because the owner consuming the decision clears the group's paused flag.

Sending such a thread a recovery decision loses the stop twice over: the
decision lands on an event nothing reads, and the stop event nobody set leaves
the thread free to finish its journey -- an end move, on real hardware, after
the operator called the run off.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    wire_system_map,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
)

_TERMINAL = frozenset({"STOPPED", "COMPLETED", "ABORTED", "FAILED"})


class _FailThenGateShaker(UniversalMockDevice):
    """First shake raises; the retried shake gates on ``release``."""

    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.calls = 0
        self.in_second_shake = asyncio.Event()
        self.release = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first shake fails")
        self.in_second_shake.set()
        await self.release.wait()
        await super().shake(duration, speed)


async def _build() -> tuple[SystemRuntime, WorkflowTemplate, _FailThenGateShaker, ISystem]:
    device = _FailThenGateShaker("shaker1", site_names=["site-1", "site-2"])
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

    @orca.action(device=pool, inputs=[plate_main, plate_child])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def parent_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def main_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield parent_method

    @orca.thread(labware=plate_child, start=pad2, end=pad2)
    async def child_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield orca.join()

    @orca.workflow(name="gated_join_wf")
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
    return runtime, workflow, device, system


@pytest.mark.asyncio
async def test_a_stop_reaches_a_contributor_paused_across_a_retry() -> None:
    runtime, workflow, device, system = await _build()
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM
        )

        def _paused_owner() -> bool:
            return any(
                t.name.startswith("plate_main") and t.status == "PAUSED"
                for t in runtime.list_threads(record.id)
            )

        await wait_until(_paused_owner, timeout=15.0)
        owner = next(
            t for t in runtime.list_threads(record.id)
            if t.name.startswith("plate_main")
        )
        contributor_id = next(
            t.id for t in runtime.list_threads(record.id)
            if t.name.startswith("plate_child")
        )

        runtime.recover_thread(record.id, owner.id, RecoveryDecision.RETRY)
        await asyncio.wait_for(device.in_second_shake.wait(), timeout=15.0)

        system.stop_all_threads()
        device.release.set()

        def _all_terminal() -> bool:
            return all(t.status in _TERMINAL for t in runtime.list_threads(record.id))

        await wait_until(_all_terminal, timeout=20.0)
        statuses = {t.id: (t.name, t.status) for t in runtime.list_threads(record.id)}
        assert statuses[contributor_id][1] == "STOPPED", (
            f"a cooperatively stopped contributor must land STOPPED; got {statuses[contributor_id][1]}"
        )
    finally:
        device.release.set()
        await runtime.shutdown()
