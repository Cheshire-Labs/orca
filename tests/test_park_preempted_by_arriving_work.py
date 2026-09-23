"""A park move blocked on an occupied slot must yield to arriving work.

The deadlock this pins (caught by the SMC adaptive beds): two racks share one
park slot. Rack A sits parked there. Rack B finishes its action and must park
before its journey reaches the next join, so it retries the blocked park move
forever. The dispatch that NEEDS rack B lands in B's slot mid-retry, but B
cannot serve it; the shared action holds the device mutex waiting for B, and
rack A stays parked until that same action frees the device. Three parties,
no cycle inside the reservation graph alone, and the retry churn keeps every
detector quiet.

``ParkTemplate`` already skips the physical move when work is queued at entry.
These tests pin the same rule continuously: work arriving while the park move
is still unresolved abandons the park (the labware rests where it is; a pad or
site is a legitimate resting spot) and the thread serves its slot.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

import orca.orca as orca
from orca.config import ReservationConfig
from orca.plugins import MethodTracker
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.reservation_manager.errors import MoveAbandonedError
from orca.system.reservation_manager.move_handler import MoveHandler
from orca.system.reservation_manager.reservation_manager import (
    ThreadReservationCoordinator,
)
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.events.execution_context import WorkflowExecutionContext
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.labware_threads.thread_state_machine import (
    LabwareThreadStatus,
    ThreadEvent,
)
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.park_template import ParkTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.test_cross_tick_deadlock import (
    _make_labware,
    _make_location_registry,
    _make_mock_thread,
    _make_thread_registry,
    _place_labware,
)
from tests.test_helpers import (
    execution_outcome,
    create_simple_system_map,
    create_test_device,
    create_test_transporter,
    wait_until,
    wire_system_map,
)
from tests.test_single_carriage_transporter import _pump


# --- Move-handler layer: the abandon predicate ---------------------------------


class TestAbandonWhen:

    @pytest.mark.asyncio
    async def test_rejected_retry_raises_when_abandon_predicate_fires(self) -> None:
        """A blocked move whose caller withdraws (predicate True) raises
        MoveAbandonedError instead of retrying, leaving no reservation behind."""
        device = create_test_device("bench", site_names=["s1"])
        arm = create_test_transporter("arm", ["bench/s1", "park_pad", "home"])
        _registry, system_map = await create_simple_system_map(
            [arm], {"bench": device},
            {"park_pad": PlatePad("park_pad"), "home": PlatePad("home")},
        )
        rack_a = _make_labware("rack_a")
        rack_b = _make_labware("rack_b")
        site = system_map.get_location("bench/s1")
        site.resource.initialize_labware(rack_a)
        _place_labware(system_map.get_location("home"), rack_b)
        locations = {n: system_map.get_location(n) for n in ("bench/s1", "park_pad", "home")}
        threads = {"thread_a": _make_mock_thread(rack_a), "thread_b": _make_mock_thread(rack_b)}
        for thread in threads.values():
            thread.thread_template = None
        registry = _make_thread_registry(threads)
        by_labware = {t.labware.id: t for t in threads.values()}
        registry.get_thread_by_labware = MagicMock(side_effect=lambda lid: by_labware[lid])
        coordinator = ThreadReservationCoordinator(
            _make_location_registry(locations), registry,
            exclusion_siblings_of=system_map.exclusion_siblings_of,
        )
        handler = MoveHandler(
            coordinator, system_map, coordinator.starvation_registry,
            ReservationConfig(retry_interval=0.02),
        )

        withdrawn = False
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", rack_a, site, [system_map.get_location("home")],
            abandon_when=lambda: withdrawn,
        ))
        await _pump(coordinator, ticks=4)
        assert not task.done(), "while the caller still wants the move it retries"

        withdrawn = True
        await _pump(coordinator, ticks=6)
        assert task.done(), "a withdrawn move must stop retrying"
        with pytest.raises(MoveAbandonedError):
            task.result()
        assert coordinator.get_active_reservations() == [], (
            "an abandoned move must leave no reservation behind"
        )


# --- ParkTemplate layer: wiring the predicate to the slot ----------------------


def _park_ctx(queue_empty: bool) -> tuple[MagicMock, MagicMock]:
    slot = MagicMock()
    slot.queue_empty = MagicMock(return_value=queue_empty)
    ctx = MagicMock()
    ctx.release_holdover = MagicMock()
    ctx.my_slot = MagicMock(return_value=slot)
    ctx.mark_my_labware_parked = MagicMock()
    park_location = MagicMock(name="park_location")
    ctx.location = MagicMock(return_value=park_location)
    ctx.current_location = MagicMock(name="elsewhere")
    ctx.fire_and_execute_move_to = AsyncMock()
    return ctx, slot


class TestParkTemplateAbandon:

    @pytest.mark.asyncio
    async def test_park_passes_slot_predicate_to_the_move(self) -> None:
        """The move layer gets a live predicate wired to the slot queue."""
        template = ParkTemplate("park_pad")
        ctx, slot = _park_ctx(queue_empty=True)

        async def _arrive_then_stop(
            event: ThreadEvent,
            target: Location,
            abandon_when: Callable[[], bool] | None = None,
        ) -> None:
            assert abandon_when is not None
            assert abandon_when() is False
            slot.queue_empty = MagicMock(return_value=False)
            assert abandon_when() is True, (
                "the predicate must read the slot LIVE, not a snapshot"
            )
            raise MoveAbandonedError("work arrived")

        ctx.fire_and_execute_move_to = AsyncMock(side_effect=_arrive_then_stop)
        yielded = [em async for em in template.schedule(ctx, MagicMock())]
        assert yielded == []
        ctx.fire_and_execute_move_to.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_park_returns_cleanly_when_move_is_abandoned(self) -> None:
        """MoveAbandonedError ends the park; the next join serves the queue."""
        template = ParkTemplate("park_pad")
        ctx, _slot = _park_ctx(queue_empty=True)
        ctx.fire_and_execute_move_to = AsyncMock(side_effect=MoveAbandonedError("x"))

        yielded = [em async for em in template.schedule(ctx, MagicMock())]
        assert yielded == []
        ctx.fire_and_execute_move_to.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multi_candidate_park_completes_at_any_declared_spot(self) -> None:
        """orca.park(["a", "b"]) is done when the labware lands on EITHER
        author-declared spot; only listed spots count."""
        template = ParkTemplate(["hotel_pad_1", "hotel_pad_2"])
        ctx, _slot = _park_ctx(queue_empty=True)
        pad_1, pad_2 = MagicMock(name="hotel_pad_1"), MagicMock(name="hotel_pad_2")
        ctx.location = MagicMock(side_effect=lambda name: {
            "hotel_pad_1": pad_1, "hotel_pad_2": pad_2,
        }[name])

        async def _land_on_second(
            event: ThreadEvent,
            target: Location,
            abandon_when: Callable[[], bool] | None = None,
        ) -> None:
            assert target == [pad_1, pad_2], (
                "every declared spot must be offered to the move layer"
            )
            ctx.current_location = pad_2

        ctx.fire_and_execute_move_to = AsyncMock(side_effect=_land_on_second)
        yielded = [em async for em in template.schedule(ctx, MagicMock())]
        assert yielded == []
        assert ctx.fire_and_execute_move_to.await_count == 1, (
            "landing on a declared spot completes the park"
        )

    @pytest.mark.asyncio
    async def test_park_rechecks_slot_between_hops(self) -> None:
        """A multi-hop park stops mid-route once work is queued."""
        template = ParkTemplate("park_pad")
        ctx, slot = _park_ctx(queue_empty=True)

        async def _hop(
            event: ThreadEvent,
            target: Location,
            abandon_when: Callable[[], bool] | None = None,
        ) -> None:
            # One hop lands somewhere that is not the park target; work
            # arrives while the labware sits on that intermediate spot.
            slot.queue_empty = MagicMock(return_value=False)

        ctx.fire_and_execute_move_to = AsyncMock(side_effect=_hop)
        yielded = [em async for em in template.schedule(ctx, MagicMock())]
        assert yielded == []
        assert ctx.fire_and_execute_move_to.await_count == 1, (
            "after a hop with work queued the park must not fire another move"
        )


# --- Full-runtime pin: the three-party park deadlock ---------------------------


async def _build_contended_park_system() -> tuple[
    ISystem, WorkflowTemplate, EventBus, asyncio.Event,
]:
    """plate owns two bench actions needing rack; rack parks between the two
    joins onto a pad a loose plate occupies, so the second dispatch lands while
    the park move is blocked."""
    bench = create_test_device("bench", site_names=["site-1", "site-2"])
    gate_station = create_test_device("gate_station")
    transporter = create_test_transporter(
        "robot1",
        ["start_plate", "start_rack", "park_pad", "end_plate", "end_rack",
         "bench", "gate_station"],
    )

    plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    rack = PlateTemplate("rack", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    registry = ResourceRegistry()
    for resource in (bench, gate_station, transporter):
        registry.add_resource(resource)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"bench": bench, "gate_station": gate_station},
        pads=["start_plate", "start_rack", "park_pad", "end_plate", "end_rack"],
    )
    release_gate = asyncio.Event()

    @orca.action(device=gate_station, inputs=[plate])
    async def hold_at_gate(ctx: ActionContext) -> None:
        await release_gate.wait()

    @orca.action(device=bench, inputs=[plate, rack])
    async def combine_first(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    @orca.action(device=bench, inputs=[plate, rack])
    async def combine_second(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=100)

    async def _gate_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield hold_at_gate
    gate_method = MethodTemplate("gate_method", func=_gate_method)

    async def _combine_first_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield combine_first
    combine_first_method = MethodTemplate("combine_first_method", func=_combine_first_method)

    async def _combine_second_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield combine_second
    combine_second_method = MethodTemplate("combine_second_method", func=_combine_second_method)

    async def _plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield combine_first_method
        yield gate_method
        yield combine_second_method

    async def _rack_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[combine_first_method])
        yield orca.park("park_pad")
        yield orca.join(allows=[combine_second_method])

    plate_thread = ThreadTemplate(
        labware_template=plate,
        start=system_map.get_location("start_plate"),
        end=system_map.get_location("end_plate"),
        func=_plate_thread,
        contributes_to=["rack"],
    )
    rack_thread = ThreadTemplate(
        labware_template=rack,
        start=system_map.get_location("start_rack"),
        end=system_map.get_location("end_rack"),
        func=_rack_thread,
    )

    workflow = WorkflowTemplate("contended_park_demo")
    workflow.add_thread(plate_thread, is_start=True)
    workflow.add_thread(rack_thread)
    # Same wiring wf.thread() does for SMC contributors: the rack spawns when
    # combine_first resolves, and combine_second reuses the live rack's slot.
    workflow.register_auto_spawn(rack_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="contended_park_system",
        description="",
        labwares=[plate, rack],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    # A loose plate left on the park pad: physically occupied, owned by no
    # thread, so the rack's park move stays rejected for the whole run. After
    # the build, which owns the world and empties it.
    _place_labware(
        system_map.get_location("park_pad"), _make_labware("left_behind"),
    )
    return system, workflow, event_bus, release_gate


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_dispatch_arriving_mid_park_is_served_not_deadlocked() -> None:
    """Rack's park is blocked by a resident on the only park pad. The combine
    dispatch lands while the park move is retrying. The rack must abandon the
    park and serve; the run must complete. Pre-fix this deadlocks: the park
    retries forever, combine never gets its rack, and the resident never leaves."""
    system, workflow, event_bus, release_gate = await _build_contended_park_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    tracker = MethodTracker()
    runtime.register_plugin(tracker)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)

        def _rack_park_blocked() -> bool:
            # Rack's only move after serving combine_first is the park; the
            # pad is occupied all run, so this wait state IS the blocked park.
            first_done = any(
                "combine_first_method" in methods
                for methods in tracker.all_completed_snapshots.values()
            )
            if not first_done:
                return False
            return any(
                t.name.startswith("rack") and t.status in (
                    "AWAITING_MOVE_RESERVATION", "AWAITING_MOVE_TARGET_AVAILABILITY",
                )
                for t in runtime.list_threads(submission.execution_id)
            )

        await wait_until(
            _rack_park_blocked, timeout=60.0,
            message="rack never reached its blocked park move",
        )
        release_gate.set()

        status = await execution_outcome(runtime, submission, timeout=90.0)
        assert status.status == "completed", (
            "a dispatch arriving mid-park must preempt the blocked park move; "
            f"got status {status.status}"
        )
    finally:
        await runtime.shutdown()

    rack_tid = next(
        tid for tid, name in tracker.thread_names.items()
        if name.startswith("rack")
    )
    rack_methods = tracker.all_completed_snapshots.get(rack_tid, [])
    assert "combine_second_method" in rack_methods, (
        f"the rack must have served the dispatch that arrived mid-park: {rack_methods}"
    )
