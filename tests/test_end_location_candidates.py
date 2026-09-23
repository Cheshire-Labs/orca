"""Thread end targets accept candidate lists: the thread ends at the first
shelf that grants, so concurrent executions' result plates take distinct
shelves instead of serializing on one (each blocking on the previous plate's
operator removal in LIVE)."""

import asyncio
from collections.abc import AsyncGenerator
from typing import cast

import pytest

import orca.orca as orca
from orca.spawn import DISPENSE, LEAVE_IN_PLACE
from orca.workflow_models.thread_template import _normalize_end, _normalize_start
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.spawn import LEAVE_IN_PLACE
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import EndArg

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wire_system_map,
)


async def _build_world() -> tuple[SystemRuntime, WorkflowTemplate, SystemMap]:
    device = UniversalMockDevice("device1")
    transporter = create_test_transporter(
        "robot1", ["device1", "start_pad", "shelf_a", "shelf_b"],
    )
    plate = create_test_plate_template("result_plate")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("device1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"device1": device},
        pads=["start_pad", "shelf_a", "shelf_b"],
    )

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    method = MethodTemplate("shake_method", func=_method)

    async def _journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    thread = ThreadTemplate(
        labware_template=plate,
        start=system_map.get_location("start_pad"),
        end=([system_map.get_location("shelf_a"),
              system_map.get_location("shelf_b")], LEAVE_IN_PLACE),
        func=_journey,
    )

    workflow = WorkflowTemplate("candidate_end_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="candidate_end_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, system_map


async def _noop_journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
    if False:
        yield


class TestEndArgValidation:

    def test_empty_candidate_list_rejected(self) -> None:
        plate = create_test_plate_template("p")
        with pytest.raises(ValueError, match="at least one location"):
            ThreadTemplate(
                labware_template=plate, start="pad", end=[],
                func=_noop_journey,
            )

    def test_sentinel_inside_candidate_list_rejected(self) -> None:
        plate = create_test_plate_template("p")
        bad_end = cast(EndArg, ["shelf_a", ("shelf_b", LEAVE_IN_PLACE)])
        with pytest.raises(ValueError, match="sentinels go on the tuple"):
            ThreadTemplate(
                labware_template=plate, start="pad", end=bad_end,
                func=_noop_journey,
            )


class TestEndLocationCandidates:

    @pytest.mark.asyncio
    async def test_two_executions_rest_on_distinct_shelves(self) -> None:
        """With LEAVE_IN_PLACE ends, the first plate keeps its shelf, so the
        second execution's grant on it is refused and the candidate list
        routes plate two to the other shelf. Neither waits on a removal."""
        runtime, workflow, system_map = await _build_world()
        await runtime.start()
        try:
            first = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            await execution_outcome(runtime, first, timeout=30.0)

            second = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            await execution_outcome(runtime, second, timeout=30.0)

            shelf_a = system_map.get_location("shelf_a").labware
            shelf_b = system_map.get_location("shelf_b").labware
            assert shelf_a is not None and shelf_b is not None, (
                f"each execution's plate keeps its own shelf; "
                f"shelf_a={shelf_a}, shelf_b={shelf_b}"
            )
            assert shelf_a.id != shelf_b.id
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_single_execution_takes_exactly_one_shelf(self) -> None:
        """Candidates never fan out: one run rests on one shelf, the other
        candidate stays free (surplus grants released)."""
        runtime, workflow, system_map = await _build_world()
        await runtime.start()
        try:
            submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            await execution_outcome(runtime, submission, timeout=30.0)
            occupied = [
                name for name in ("shelf_a", "shelf_b")
                if system_map.get_location(name).labware is not None
            ]
            assert len(occupied) == 1
        finally:
            await runtime.shutdown()


def test_sentinel_written_inside_the_candidate_list_is_refused() -> None:
    """`end=[...spots, LEAVE_IN_PLACE]` must fail at build time, naming the fix.

    Sentinels are plain strings, so without this the sentinel is accepted as a
    candidate LOCATION named "leave_in_place": the thread silently keeps the
    default manual-remove intent and dies much later on an unknown location,
    with the author's actual intent lost.
    """
    with pytest.raises(ValueError) as exc:
        _normalize_end(["hotel_pad_1", LEAVE_IN_PLACE])
    assert "spawn sentinel" in str(exc.value)
    assert "end=([...], LEAVE_IN_PLACE)" in str(exc.value)

    ends, flags = _normalize_end((["hotel_pad_1", "hotel_pad_2"], LEAVE_IN_PLACE))
    assert flags.leave_in_place is True
    assert ends == ["hotel_pad_1", "hotel_pad_2"]


def test_start_sentinel_written_as_the_location_is_refused() -> None:
    """`start=DISPENSE` must fail the same way `end=[..., LEAVE_IN_PLACE]` does.

    Sentinels are plain strings on both sides, so the bare form silently
    became a location named "dispense" with the manual-place default kept.
    Guarding one side and not the other left the same trap thirty lines away.
    """
    with pytest.raises(ValueError) as exc:
        _normalize_start(DISPENSE)
    assert "start=(location, DISPENSE)" in str(exc.value)

    loc, flags = _normalize_start(("stacker_1", DISPENSE))
    assert flags.dispense is True and loc == "stacker_1"


def test_wrong_side_sentinel_names_the_side_that_accepts_it() -> None:
    """The error must point at a form that WORKS.

    `end=[..., DISPENSE]` telling the author to write `end=([...], DISPENSE)`
    sends them into a second failure, since DISPENSE is not a valid end
    sentinel.
    """
    with pytest.raises(ValueError) as exc:
        _normalize_end(["hotel_pad_1", DISPENSE])
    message = str(exc.value)
    assert "start-side sentinel" in message
    assert "start=(location, DISPENSE)" in message
    assert "end=([...], DISPENSE)" not in message
