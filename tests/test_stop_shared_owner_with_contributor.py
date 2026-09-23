"""A cooperative stop of a shared-action owner together with its contributor
(whole-execution teardown) lands every participant terminal without wedging.

A contributor parked awaiting the owner's shared-action outcome must honor its
own stop_event even though the co-torn-down owner publishes an ABORT_METHOD
outcome on the action group. Otherwise it follows that outcome back into a
non-yielding loop on a ``completed`` the owner's stop never sets, starving the
event loop instead of landing STOPPED.

The owner's stop DOES still publish that outcome on purpose: it wakes a
contributor parked pre-binding in ``wait_for_current_action`` (which does not
watch stop_event). The fix is that a post-binding contributor's own stop wins
over the published outcome, not that the publish is removed.
"""

import asyncio
from collections.abc import AsyncGenerator

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
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    wire_system_map,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
)

_TERMINAL = frozenset({"STOPPED", "COMPLETED", "ABORTED"})


class _GatedShaker(UniversalMockDevice):
    """Shake blocks until ``release`` is set. ``in_shake`` lets a test wait
    until the owner is firmly in the device op (past its co-labware wait, so the
    contributor is already parked awaiting the shared outcome)."""

    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.in_shake = asyncio.Event()
        self.release = asyncio.Event()

    async def shake(self, duration: int, speed: int) -> None:
        self.in_shake.set()
        await self.release.wait()
        await super().shake(duration, speed)


async def _build_gated_join_system() -> tuple[
    SystemRuntime, WorkflowTemplate, _GatedShaker, ISystem
]:
    """One owner runs a shared shake; a contributor joins it. The shake gates on
    ``release`` so a test can stop both threads while the action is mid-flight."""
    # Two-input shared action: one working site per simultaneously-present labware.
    device = _GatedShaker("shaker1", site_names=["site-1", "site-2"])
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


async def test_cooperative_stop_of_shared_owner_and_contributor_lands_terminal() -> None:
    runtime, workflow, device, system = await _build_gated_join_system()
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            workflow.name, mode=WorkflowRunMode.PURE_SIM
        )

        # A contributor parked in _follow_shared_action_outcome reports
        # EXECUTING_ACTION (it fires ACTION_RESOLVED on entry), like the owner.
        await asyncio.wait_for(device.in_shake.wait(), timeout=10.0)

        def _contributor_following() -> bool:
            return any(
                t.name.startswith("plate_child") and t.status == "EXECUTING_ACTION"
                for t in runtime.list_threads(record.id)
            )

        await wait_until(_contributor_following, timeout=10.0)
        contributor_id = next(
            t.id
            for t in runtime.list_threads(record.id)
            if t.name.startswith("plate_child")
        )

        # Whole-execution teardown: stop every live thread cooperatively, then
        # let the owner's blocked device op finish so it can finalize.
        system.stop_all_threads()
        device.release.set()

        def _all_terminal() -> bool:
            return all(
                t.status in _TERMINAL for t in runtime.list_threads(record.id)
            )

        await wait_until(_all_terminal, timeout=10.0)

        statuses = {t.id: t.status for t in runtime.list_threads(record.id)}
        assert statuses[contributor_id] == "STOPPED", (
            "a cooperatively stopped contributor must land STOPPED, not follow "
            "the co-torn-down owner's ABORT_METHOD outcome into a hot loop; got "
            f"{statuses[contributor_id]}"
        )
    finally:
        device.release.set()
        await runtime.shutdown()
