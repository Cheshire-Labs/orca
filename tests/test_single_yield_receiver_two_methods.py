"""Regression: single-yield receiver thread serving two sequential methods.

Pins the contract that EVERY receiver thread instance spawned for a
single-yield-join template visits the device location at least once.

Pre-fix (Bug 3 / PLR labware_journeys failure shape): the slot binding
logic at ``_make_auto_spawn_callback`` saw ``slot.has_active_thread() ==
True`` for the leaving receiver and routed method_b's contribution to
it. The bound method sat in ``slot.queue``; the owner completed method_b
without the contributor needing to physically arrive (the existing
receiver's labware was still at the device). When the receiver thread
later reached its end_location, ``drain_for_handoff`` re-routed the
already-completed method_b to a fresh receiver. That fresh receiver
spawned, started method_b, immediately exited its method loop
(``_assigned_method.completed.is_set() == True``), and marched
``stacker -> robotic_arm/gripper -> waste`` with no device visit.

Post-fix: ``drain_for_handoff`` skips already-completed methods, so
the spurious fresh-receiver spawn does not happen when the owner has
already finished method_b against the existing receiver's labware.

The test allows for one OR two receiver thread instances:
- ONE if method_b completes before the first receiver leaves (the
  completed method is skipped by drain_for_handoff; no fresh spawn).
- TWO if method_b is still pending when drain runs (legitimate fresh
  spawn; second receiver moves to station and processes method_b).
The invariant the test pins is that NEITHER receiver fails to visit
the device.
"""

import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

import orca.orca as orca
from orca.plugins import LabwareJourneyTracker, MethodTracker
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import IMethodTemplate
from orca.runtime.run_modes import WorkflowRunMode
from tests.test_helpers import (
    execution_outcome,
    create_test_device,
    create_test_transporter,
    wire_system_map,
)


async def _build_two_method_single_yield_receiver_system() -> tuple[
    ISystem, WorkflowTemplate, EventBus,
]:
    """Owner runs method_a + method_b; receiver single-yields join over both."""
    station = create_test_device("station", site_names=["site-1", "site-2"])
    transporter = create_test_transporter(
        "robot1", ["start_pad", "station", "waste"],
    )

    owner_plate = PlateTemplate("owner", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    receiver_plate = PlateTemplate(
        "receiver", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
    )

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    registry.add_resource_pool(station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"station": station},
        pads=["start_pad", "waste"],
    )

    @orca.action(device=station_pool, inputs=[owner_plate, receiver_plate])
    async def step_a(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=station_pool, inputs=[owner_plate, receiver_plate])
    async def step_b(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=600)

    async def _method_a_func(
        ctx: object,
    ) -> AsyncGenerator[ActionTemplate, None]:
        yield step_a
    method_a = MethodTemplate("method_a", func=_method_a_func)

    async def _method_b_func(
        ctx: object,
    ) -> AsyncGenerator[ActionTemplate, None]:
        yield step_b
    method_b = MethodTemplate("method_b", func=_method_b_func)

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _owner_thread_func(
        ctx: object,
    ) -> AsyncGenerator[MethodTemplate, None]:
        yield method_a
        yield method_b
    owner_thread = ThreadTemplate(
        labware_template=owner_plate,
        start=start_pad,
        end=waste,
        func=_owner_thread_func,
    )

    async def _receiver_thread_func(
        ctx: object,
    ) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[method_a, method_b])
    receiver_thread = ThreadTemplate(
        labware_template=receiver_plate,
        start=start_pad,
        end=waste,
        func=_receiver_thread_func,
    )

    workflow = WorkflowTemplate("two_method_single_yield_receiver")
    workflow.add_thread(owner_thread, is_start=True)
    workflow.add_thread(receiver_thread)
    workflow.register_auto_spawn(receiver_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="two_method_single_yield_receiver_system",
        description="",
        labwares=[owner_plate, receiver_plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def _owner_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="owner"),),
    )


class TestSingleYieldReceiverTwoMethods:

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_every_receiver_visits_station(self) -> None:
        """Every spawned receiver instance visits the station device.

        Regression for Bug 3: pre-fix, a fresh second receiver could spawn
        AFTER method_b had already completed (re-routed by
        drain_for_handoff). That receiver never moved to the station --
        its journey was start_pad -> gripper -> waste. Post-fix, either
        no second receiver spawns (method_b already completed -> skipped
        in drain), OR a second receiver spawns legitimately and visits
        the station. Both branches satisfy the "no zombie receiver"
        invariant pinned here.
        """
        system, workflow, event_bus = (
            await _build_two_method_single_yield_receiver_system()
        )
        runtime = SystemRuntime(system, event_bus=event_bus)
        method_tracker = MethodTracker()
        journey_tracker = LabwareJourneyTracker()
        runtime.register_plugin(method_tracker)
        runtime.register_plugin(journey_tracker)
        await runtime.start()
        try:
            submission = await runtime.submit(
                workflow, groups=[_owner_group()],
                mode=WorkflowRunMode.PURE_SIM,
            )
            status = await execution_outcome(runtime, submission, timeout=60.0)
        finally:
            await runtime.shutdown()
        assert status.status == "completed"

        receiver_names = [
            name for name in method_tracker.thread_names.values()
            if name.startswith("receiver")
        ]
        assert len(receiver_names) >= 1, (
            f"At least 1 receiver thread expected; got 0"
        )

        receiver_journeys = [
            j for tid, j in journey_tracker.all_completed_journeys.items()
            if journey_tracker.thread_names.get(tid, "").startswith("receiver")
        ]
        # A zombie receiver never visits a station site (Bug 3 shape); undeclared
        # inputs land on ANY free site, so match by device prefix, not exact site.
        for i, journey in enumerate(receiver_journeys):
            assert any(step.startswith("station/") for step in journey), (
                f"receiver[{i}] never visited a station site. "
                f"Journey: {' -> '.join(journey)}"
            )
