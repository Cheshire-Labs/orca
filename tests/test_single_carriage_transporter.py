"""Single-carriage transporters: one labware across ALL taught positions.

A translator is one physical carriage on a rail; its two endpoints are the
same pad at two positions. The two-pad graph model let both endpoints fill
at once, so opposing plates met head-on mid-bridge -- an unresolvable,
physically impossible state that wedged the N=6 SMC batch (both endpoints
occupied, all nearby pads camped, mutex holder stranded behind the bridge).

Declaring ``single_carriage=True`` on a transporter makes the reservation
layer reject boarding any of its positions while another thread's labware
occupies -- or another thread reserves -- a sibling position. The deadlock
detector casts wait-for edges through the same sibling rule so bridge waits
stay visible to knot detection.
"""
import asyncio
from unittest.mock import Mock

import pytest

from orca.config import ReservationConfig
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.system.reservation_manager.move_handler import (
    MoveActionCollectionReservationRequest,
    MoveHandler,
)
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
    ThreadReservationCoordinator,
)
from orca.system.system_map import SystemMap
from orca.workflow_models.actions.move_action import MoveAction
from tests.test_cross_tick_deadlock import (
    _make_collection,
    _make_labware,
    _make_location,
    _make_location_registry,
    _make_mock_thread,
    _make_thread_registry,
    _place_labware,
)
from tests.test_helpers import (
    create_simple_system_map,
    create_test_device,
    create_test_transporter,
)


def _bridge_pads() -> dict[str, PlatePad]:
    return {
        "t1_start": PlatePad("t1_start", supports_deadlock_resolution=False),
        "t1_end": PlatePad("t1_end", supports_deadlock_resolution=False),
        "pad_1": PlatePad("pad_1"),
    }


class TestSystemMapRegistration:

    @pytest.mark.asyncio
    async def test_single_carriage_transporter_registers_sibling_group(self) -> None:
        translator = create_test_transporter(
            "translator_1", ["t1_start", "t1_end"], single_carriage=True
        )
        arm = create_test_transporter("arm", ["pad_1", "t1_start", "t1_end"])
        _registry, system_map = await create_simple_system_map(
            [translator, arm], {}, _bridge_pads()
        )

        siblings = system_map.exclusion_siblings_of("t1_start")
        assert [loc.position_id for loc in siblings] == ["t1_end"]
        assert system_map.exclusion_siblings_of("pad_1") == []

    @pytest.mark.asyncio
    async def test_default_transporter_registers_no_group(self) -> None:
        arm = create_test_transporter("arm", ["t1_start", "t1_end", "pad_1"])
        _registry, system_map = await create_simple_system_map([arm], {}, _bridge_pads())
        assert system_map.exclusion_siblings_of("t1_start") == []

    @pytest.mark.asyncio
    async def test_overlapping_single_carriage_groups_fail_loud(self) -> None:
        shuttle_1 = create_test_transporter(
            "shuttle_1", ["t1_start", "t1_end"], single_carriage=True
        )
        shuttle_2 = create_test_transporter(
            "shuttle_2", ["t1_end", "pad_1"], single_carriage=True
        )
        with pytest.raises(ValueError, match="single_carriage"):
            await create_simple_system_map([shuttle_1, shuttle_2], {}, _bridge_pads())


def _bridge_world() -> tuple[LocationReservationManager, dict[str, Location]]:
    """A start/end sibling pair plus a free pad, with sibling exclusion wired."""
    locations = {
        "t1_start": _make_location("t1_start"),
        "t1_end": _make_location("t1_end"),
        "pad_1": _make_location("pad_1"),
    }
    siblings = {
        "t1_start": [locations["t1_end"]],
        "t1_end": [locations["t1_start"]],
    }
    manager = LocationReservationManager(
        _make_location_registry(locations),
        exclusion_siblings_of=lambda pid: siblings.get(pid, []),
    )
    return manager, locations


class TestReservationGate:

    @pytest.mark.asyncio
    async def test_boarding_rejected_while_sibling_occupied(self) -> None:
        manager, locations = _bridge_world()
        camper = _make_labware("camper")
        _place_labware(locations["t1_end"], camper)

        boarder = LocationReservation(locations["t1_start"], _make_labware("boarder"))
        await manager.attempt_reservation("t1_start", boarder, thread_id="t2")
        assert boarder.rejected.is_set(), (
            "t1_start is empty but the carriage is at t1_end under another "
            "plate; boarding must reject or the bridge holds two plates"
        )

    @pytest.mark.asyncio
    async def test_crossing_plate_not_blocked_by_its_own_occupancy(self) -> None:
        manager, locations = _bridge_world()
        crosser = _make_labware("crosser")
        _place_labware(locations["t1_start"], crosser)

        request = LocationReservation(locations["t1_end"], crosser)
        await manager.attempt_reservation("t1_end", request, thread_id="t1")
        assert request.granted.is_set(), (
            "the plate ON the carriage must be able to reserve the opposite "
            "endpoint to ride across"
        )

    @pytest.mark.asyncio
    async def test_boarding_rejected_while_sibling_reserved_by_other_thread(self) -> None:
        manager, locations = _bridge_world()
        inbound = LocationReservation(locations["t1_end"], _make_labware("inbound"))
        await manager.attempt_reservation("t1_end", inbound, thread_id="t1")
        assert inbound.granted.is_set()

        boarder = LocationReservation(locations["t1_start"], _make_labware("boarder"))
        await manager.attempt_reservation("t1_start", boarder, thread_id="t2")
        assert boarder.rejected.is_set(), (
            "a mid-flight crossing holds the sibling reservation; boarding the "
            "other endpoint must wait for it to alight"
        )

    @pytest.mark.asyncio
    async def test_ungrouped_pad_unaffected_by_occupied_bridge(self) -> None:
        manager, locations = _bridge_world()
        _place_labware(locations["t1_end"], _make_labware("camper"))

        pad_request = LocationReservation(locations["pad_1"], _make_labware("free"))
        await manager.attempt_reservation("pad_1", pad_request, thread_id="t9")
        assert pad_request.granted.is_set(), (
            "pad_1 is in no group; the sibling rule must not leak"
        )

    @pytest.mark.asyncio
    async def test_empty_bridge_grants_boarding(self) -> None:
        manager, locations = _bridge_world()
        boarder = LocationReservation(locations["t1_start"], _make_labware("boarder"))
        await manager.attempt_reservation("t1_start", boarder, thread_id="t2")
        assert boarder.granted.is_set()


class TestDetectorSiblingEdges:
    """A sibling-blocked wait must cast a wait-for edge to the camper.

    plate_x (on pad_1) requests t1_start: the position is EMPTY -- only the
    sibling rule rejects it. Without a sibling edge the detector sees a free
    candidate, prunes plate_x as escapable, and the head-on knot with the
    camper (which waits for pad_1) is never detected.
    """

    @pytest.mark.asyncio
    async def test_sibling_blocked_swap_is_detected_and_flagged(self) -> None:
        plate_x = _make_labware("plate_x")
        plate_y = _make_labware("plate_y")
        locations = {
            "t1_start": _make_location("t1_start"),
            "t1_end": _make_location("t1_end"),
            "pad_1": _make_location("pad_1"),
        }
        _place_labware(locations["pad_1"], plate_x)
        _place_labware(locations["t1_end"], plate_y)
        siblings = {
            "t1_start": [locations["t1_end"]],
            "t1_end": [locations["t1_start"]],
        }
        coordinator = ThreadReservationCoordinator(
            _make_location_registry(locations),
            _make_thread_registry({
                "thread_x": _make_mock_thread(plate_x),
                "thread_y": _make_mock_thread(plate_y),
            }),
            exclusion_siblings_of=lambda pid: siblings.get(pid, []),
        )

        col_x = _make_collection("thread_x", plate_x, locations["pad_1"], locations["t1_start"])
        async with coordinator._lock:
            coordinator._queue.append(col_x)
        await coordinator._on_tick()
        assert col_x.rejected.is_set(), "sibling rule must reject boarding"

        col_y = _make_collection("thread_y", plate_y, locations["t1_end"], locations["pad_1"])
        async with coordinator._lock:
            coordinator._queue.append(col_y)
        await coordinator._on_tick()
        assert col_y.rejected.is_set()

        flagged = coordinator._deadlock_detector._deadlocked_threads
        assert len(flagged) == 1 and flagged <= {"thread_x", "thread_y"}, (
            f"sibling-blocked swap must flag one waiter; flagged={flagged}"
        )


async def _corridor_world() -> tuple[
    MoveHandler, ThreadReservationCoordinator, SystemMap, dict[str, LabwareInstance]
]:
    """far_pad/far_pad_2 -[arm_far]- t1_start =[translator]= t1_end -[arm_near]- home/near_pad.

    rack_a sits at far_pad and wants home; rack_b holds home; plate_c sits at
    near_pad. The staging-deadlock shape: with home occupied, boarding the
    translator strands rack_a on the carriage and severs the bridge.
    """
    translator = create_test_transporter(
        "translator_1", ["t1_start", "t1_end"], single_carriage=True
    )
    arm_far = create_test_transporter("arm_far", ["far_pad", "far_pad_2", "t1_start"])
    arm_near = create_test_transporter("arm_near", ["t1_end", "home", "home_2", "near_pad"])
    pads = {
        "far_pad": PlatePad("far_pad"),
        "far_pad_2": PlatePad("far_pad_2"),
        "t1_start": PlatePad("t1_start", supports_deadlock_resolution=False),
        "t1_end": PlatePad("t1_end", supports_deadlock_resolution=False),
        "home": PlatePad("home"),
        "home_2": PlatePad("home_2"),
        "near_pad": PlatePad("near_pad"),
    }
    _registry, system_map = await create_simple_system_map(
        [translator, arm_far, arm_near], {}, pads
    )
    rack_a = _make_labware("rack_a")
    rack_b = _make_labware("rack_b")
    plate_c = _make_labware("plate_c")
    locations = {name: system_map.get_location(name) for name in pads}
    _place_labware(locations["far_pad"], rack_a)
    _place_labware(locations["home"], rack_b)
    _place_labware(locations["near_pad"], plate_c)
    threads = {
        "thread_a": _make_mock_thread(rack_a),
        "thread_b": _make_mock_thread(rack_b),
        "thread_c": _make_mock_thread(plate_c),
    }
    for thread in threads.values():
        thread.thread_template = None
    thread_registry = _make_thread_registry(threads)
    threads_by_labware = {thread.labware.id: thread for thread in threads.values()}
    thread_registry.get_thread_by_labware = Mock(
        side_effect=lambda labware_id: threads_by_labware[labware_id]
    )
    coordinator = ThreadReservationCoordinator(
        _make_location_registry(locations),
        thread_registry,
        exclusion_siblings_of=system_map.exclusion_siblings_of,
    )
    handler = MoveHandler(
        coordinator,
        system_map,
        coordinator.starvation_registry,
        ReservationConfig(retry_interval=0.02),
    )
    return handler, coordinator, system_map, {
        "rack_a": rack_a, "rack_b": rack_b, "plate_c": plate_c,
    }


async def _pump(coordinator: ThreadReservationCoordinator, ticks: int = 5) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0.03)
        await coordinator._on_tick()
    await asyncio.sleep(0)


async def _drain(*tasks: asyncio.Task[MoveAction]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class TestBoardingRequiresOnwardHop:
    """A carriage position is a corridor, not a destination: boarding is
    granted only together with the onward hop, so labware can never rest on
    the carriage waiting for somewhere to go. This is the reserve-destination
    rule every move already follows, applied across the corridor instead of
    stopping at its seat.
    """

    @pytest.mark.asyncio
    async def test_boarding_denied_while_onward_hop_occupied(self) -> None:
        handler, coordinator, system_map, labware = await _corridor_world()
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [system_map.get_location("home")],
        ))
        await _pump(coordinator)
        try:
            assert not task.done(), (
                "home is occupied, so boarding the translator would strand "
                "rack_a on the carriage; the move must wait at far_pad"
            )
        finally:
            await _drain(task)

    @pytest.mark.asyncio
    async def test_boarding_grants_with_onward_hops_reserved(self) -> None:
        handler, coordinator, system_map, labware = await _corridor_world()
        home = system_map.get_location("home")
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [home],
        ))
        await _pump(coordinator)
        assert not task.done()

        await home.resource.notify_picked(labware["rack_b"], Mock())
        await _pump(coordinator, ticks=10)
        assert task.done(), "with home free, boarding must be granted"
        move = task.result()
        assert move.target.position_id == "t1_start"
        active = {
            (position_id, thread_id)
            for position_id, _reservation_id, thread_id in coordinator.get_active_reservations()
        }
        assert ("t1_end", "thread_a") in active, (
            "boarding must carry the far seat reservation so the crossing "
            "cannot be cut off mid-corridor"
        )
        assert ("home", "thread_a") in active, (
            "boarding must carry the onward hop reservation; without it the "
            "rack can be stranded on the carriage"
        )

    @pytest.mark.asyncio
    async def test_waiting_rack_leaves_the_carriage_free_for_others(self) -> None:
        handler, coordinator, system_map, labware = await _corridor_world()
        task_a = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [system_map.get_location("home")],
        ))
        await _pump(coordinator)
        try:
            assert not task_a.done(), "rack_a must wait off the carriage"
            task_c = asyncio.create_task(handler.resolve_move_action(
                "thread_c", labware["plate_c"],
                system_map.get_location("near_pad"),
                [system_map.get_location("far_pad_2")],
            ))
            await _pump(coordinator, ticks=10)
            assert task_c.done(), (
                "with rack_a waiting at far_pad instead of parked on the "
                "carriage, plate_c must be able to board and cross"
            )
            assert task_c.result().target.position_id == "t1_end"
        finally:
            await _drain(task_a)

    @pytest.mark.asyncio
    async def test_one_occupied_terminal_does_not_veto_interchangeable_candidates(self) -> None:
        handler, coordinator, system_map, labware = await _corridor_world()
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [system_map.get_location("home"), system_map.get_location("home_2")],
        ))
        await _pump(coordinator, ticks=10)
        assert task.done(), (
            "home is occupied but home_2 is an interchangeable free candidate; "
            "boarding must grant on the free terminal, not veto the crossing"
        )
        assert task.result().target.position_id == "t1_start"
        active = {
            (position_id, thread_id)
            for position_id, _reservation_id, thread_id in coordinator.get_active_reservations()
        }
        assert ("home_2", "thread_a") in active
        assert ("home", "thread_a") not in active, (
            "the occupied candidate must not be held; one terminal is enough"
        )

    @pytest.mark.asyncio
    async def test_held_terminal_is_consumed_not_orphaned(self) -> None:
        handler, coordinator, system_map, labware = await _corridor_world()
        home = system_map.get_location("home")
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [home, system_map.get_location("home_2")],
        ))
        await _pump(coordinator, ticks=10)
        assert task.done() and task.result().target.position_id == "t1_start"

        # home frees AFTER boarding granted on home_2; the crossing leg
        # re-resolves with both candidates and must not double-hold them.
        await home.resource.notify_picked(labware["rack_b"], Mock())
        crossing = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("t1_start"),
            [home, system_map.get_location("home_2")],
        ))
        await _pump(coordinator, ticks=10)
        assert crossing.done()
        assert crossing.result().target.position_id == "t1_end"
        held_terminals = {
            position_id
            for position_id, _reservation_id, thread_id in coordinator.get_active_reservations()
            if thread_id == "thread_a" and position_id in {"home", "home_2"}
        }
        assert len(held_terminals) == 1, (
            f"one crossing needs exactly one terminal hold, got {held_terminals}: "
            "a second hold starves the sibling; zero orphans the plate mid-corridor"
        )

    @pytest.mark.asyncio
    async def test_stale_corridor_holds_swept_at_rest(self) -> None:
        handler, coordinator, system_map, labware = await _corridor_world()
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [system_map.get_location("home_2")],
        ))
        await _pump(coordinator, ticks=10)
        assert task.done()

        # Route change while still at rest (e.g. a thread mutation): the next
        # resolve from a resting position must release the unused insurance.
        retarget = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [system_map.get_location("far_pad_2")],
        ))
        await _pump(coordinator, ticks=10)
        assert retarget.done()
        active_positions = {
            position_id
            for position_id, _reservation_id, thread_id in coordinator.get_active_reservations()
            if thread_id == "thread_a"
        }
        assert "home_2" not in active_positions and "t1_end" not in active_positions, (
            "abandoned corridor holds must be swept once the labware rests "
            "off the carriage, or they block the positions forever"
        )

    @pytest.mark.asyncio
    async def test_route_change_on_the_carriage_releases_dropped_holds(self) -> None:
        """A dispatch can retarget a thread mid-crossing (e.g. an abandoned
        park). The crossing consumes holds it re-crowns via displacement, but a
        hold the new route simply DROPS must be released, not just forgotten:
        an orphaned grant blocks that position for every other thread forever."""
        handler, coordinator, system_map, labware = await _corridor_world()
        home = system_map.get_location("home")
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [system_map.get_location("home_2")],
        ))
        await _pump(coordinator, ticks=10)
        assert task.done() and task.result().target.position_id == "t1_start"

        await home.resource.notify_picked(labware["rack_b"], Mock())
        retargeted = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("t1_start"),
            [home],
        ))
        await _pump(coordinator, ticks=10)
        assert retargeted.done()
        assert retargeted.result().target.position_id == "t1_end"
        active_positions = {
            position_id
            for position_id, _reservation_id, thread_id in coordinator.get_active_reservations()
            if thread_id == "thread_a"
        }
        assert "home_2" not in active_positions, (
            "the retargeted crossing dropped the home_2 insurance; leaving its "
            "grant alive squats the position for every other thread"
        )

    @pytest.mark.asyncio
    async def test_starved_move_from_device_site_vacates_to_a_pad(self) -> None:
        device = create_test_device("bravo_x", site_names=["s1"])
        arm = create_test_transporter("arm", ["bravo_x/s1", "park_pad", "home"])
        _registry, system_map = await create_simple_system_map(
            [arm], {"bravo_x": device},
            {"park_pad": PlatePad("park_pad"), "home": PlatePad("home")},
        )
        rack_a = _make_labware("rack_a")
        rack_b = _make_labware("rack_b")
        site = system_map.get_location("bravo_x/s1")
        site.resource.initialize_labware(rack_a)
        _place_labware(system_map.get_location("home"), rack_b)
        locations = {n: system_map.get_location(n) for n in ("bravo_x/s1", "park_pad", "home")}
        threads = {"thread_a": _make_mock_thread(rack_a), "thread_b": _make_mock_thread(rack_b)}
        for thread in threads.values():
            thread.thread_template = None
        registry = _make_thread_registry(threads)
        by_labware = {t.labware.id: t for t in threads.values()}
        registry.get_thread_by_labware = Mock(side_effect=lambda lid: by_labware[lid])
        coordinator = ThreadReservationCoordinator(
            _make_location_registry(locations), registry,
            exclusion_siblings_of=system_map.exclusion_siblings_of,
        )
        # The boundary moves, not the clock: a pumped tick count is not a
        # wall-clock duration once the runner is loaded.
        config = ReservationConfig(retry_interval=0.02, site_vacate_patience=3600.0)
        handler = MoveHandler(
            coordinator, system_map, coordinator.starvation_registry, config,
        )

        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", rack_a, site, [system_map.get_location("home")],
        ))
        await _pump(coordinator, ticks=3)
        assert not task.done(), "within patience the plate waits on the site"
        config.site_vacate_patience = 0.001
        await _pump(coordinator, ticks=12)
        assert task.done(), (
            "after patience a move starved on a device-owned site must widen "
            "to the resolution pads and clear the shared deck"
        )
        assert task.result().target.position_id == "park_pad"

        # Re-resolving the same starved episode must not re-offer the visited
        # pad, or the plate ping-pongs site<->pad on real hardware forever.
        second = asyncio.create_task(handler.resolve_move_action(
            "thread_a", rack_a, site, [system_map.get_location("home")],
        ))
        await _pump(coordinator, ticks=15)
        assert not second.done(), (
            "the same episode must not vacate to the same pad twice; the "
            "starved move waits instead of oscillating"
        )
        await _drain(second)

    @pytest.mark.asyncio
    async def test_abandonable_move_starved_at_rest_keeps_waiting(self) -> None:
        """An abandonable move starved while its labware rests on a pad WAITS:
        parked labware is never relocated and a park completes only at an
        author-declared spot (owner decision, 2026-08-10). The park still ends
        the moment work arrives (abandon_when); a system-wide wedge is the
        stall detector's to surface, not the move layer's to improvise around."""
        device = create_test_device("bravo_x", site_names=["s1"])
        arm = create_test_transporter("arm", ["bravo_x/s1", "park_pad", "home"])
        _registry, system_map = await create_simple_system_map(
            [arm], {"bravo_x": device},
            {"park_pad": PlatePad("park_pad"), "home": PlatePad("home")},
        )
        rack_a = _make_labware("rack_a")
        rack_b = _make_labware("rack_b")
        _place_labware(system_map.get_location("park_pad"), rack_a)
        _place_labware(system_map.get_location("home"), rack_b)
        locations = {n: system_map.get_location(n) for n in ("bravo_x/s1", "park_pad", "home")}
        threads = {"thread_a": _make_mock_thread(rack_a), "thread_b": _make_mock_thread(rack_b)}
        for thread in threads.values():
            thread.thread_template = None
        registry = _make_thread_registry(threads)
        by_labware = {t.labware.id: t for t in threads.values()}
        registry.get_thread_by_labware = Mock(side_effect=lambda lid: by_labware[lid])
        coordinator = ThreadReservationCoordinator(
            _make_location_registry(locations), registry,
            exclusion_siblings_of=system_map.exclusion_siblings_of,
        )
        handler = MoveHandler(
            coordinator, system_map, coordinator.starvation_registry,
            ReservationConfig(retry_interval=0.02, site_vacate_patience=0.3),
        )

        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", rack_a, system_map.get_location("park_pad"),
            [system_map.get_location("home")],
            abandon_when=lambda: False,
        ))
        await _pump(coordinator, ticks=15)
        try:
            assert not task.done(), (
                "a starved at-rest park must keep waiting for its declared "
                "home, never end elsewhere"
            )
        finally:
            await _drain(task)

    @pytest.mark.asyncio
    async def test_carriage_as_final_target_needs_no_onward_hop(self) -> None:
        handler, coordinator, system_map, labware = await _corridor_world()
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("far_pad"),
            [system_map.get_location("t1_start")],
        ))
        await _pump(coordinator, ticks=10)
        assert task.done(), (
            "a move whose requested target IS the carriage position has no "
            "onward hop to require; the empty bridge must grant it"
        )
        assert task.result().target.position_id == "t1_start"

    @pytest.mark.asyncio
    async def test_boarding_onward_positions_walk_to_first_resting_position(self) -> None:
        _handler, _coordinator, system_map, _labware = await _corridor_world()
        assert system_map.boarding_onward_positions(
            ["far_pad", "t1_start", "t1_end", "home"]
        ) == ["t1_end", "home"]
        assert system_map.boarding_onward_positions(["near_pad", "home"]) == []
        assert system_map.boarding_onward_positions(["far_pad", "t1_start"]) == []


async def _dual_bridge_world() -> tuple[
    MoveHandler, ThreadReservationCoordinator, SystemMap, dict[str, LabwareInstance]
]:
    """src_pad -[arm_far]- {tA_in =A= tA_out | tB_in =B= tB_out} -[arm_near]- dest/dest_2.

    Two single-carriage bridges to one destination, so both routes' boarding
    runs terminate at ``dest``. rack_b squats bridge B's exit seat; bridge A
    is completely free."""
    translator_a = create_test_transporter(
        "translator_a", ["tA_in", "tA_out"], single_carriage=True
    )
    translator_b = create_test_transporter(
        "translator_b", ["tB_in", "tB_out"], single_carriage=True
    )
    arm_far = create_test_transporter("arm_far", ["src_pad", "tA_in", "tB_in"])
    arm_near = create_test_transporter("arm_near", ["tA_out", "tB_out", "dest", "dest_2"])
    pads = {
        "src_pad": PlatePad("src_pad"),
        "tA_in": PlatePad("tA_in", supports_deadlock_resolution=False),
        "tA_out": PlatePad("tA_out", supports_deadlock_resolution=False),
        "tB_in": PlatePad("tB_in", supports_deadlock_resolution=False),
        "tB_out": PlatePad("tB_out", supports_deadlock_resolution=False),
        "dest": PlatePad("dest"),
        "dest_2": PlatePad("dest_2"),
    }
    _registry, system_map = await create_simple_system_map(
        [translator_a, translator_b, arm_far, arm_near], {}, pads
    )
    rack_a = _make_labware("rack_a")
    rack_b = _make_labware("rack_b")
    locations = {name: system_map.get_location(name) for name in pads}
    _place_labware(locations["src_pad"], rack_a)
    _place_labware(locations["tB_out"], rack_b)
    threads = {
        "thread_a": _make_mock_thread(rack_a),
        "thread_b": _make_mock_thread(rack_b),
    }
    for thread in threads.values():
        thread.thread_template = None
    thread_registry = _make_thread_registry(threads)
    threads_by_labware = {t.labware.id: t for t in threads.values()}
    thread_registry.get_thread_by_labware = Mock(
        side_effect=lambda labware_id: threads_by_labware[labware_id]
    )
    coordinator = ThreadReservationCoordinator(
        _make_location_registry(locations),
        thread_registry,
        exclusion_siblings_of=system_map.exclusion_siblings_of,
    )
    handler = MoveHandler(
        coordinator, system_map, coordinator.starvation_registry,
        ReservationConfig(retry_interval=0.02),
    )
    return handler, coordinator, system_map, {"rack_a": rack_a, "rack_b": rack_b}


class TestSiblingRoutesSharingATerminal:
    """Candidate routes to one destination carry the SAME terminal position.
    Each position must resolve to ONE reservation object across the
    collection: separate same-thread objects displace each other at attempt
    time, and the displaced free route then rejects the whole collection on
    every retry, forever (production move timeout is None)."""

    @pytest.mark.asyncio
    async def test_free_bridge_grants_when_routes_share_the_terminal(self) -> None:
        handler, coordinator, system_map, labware = await _dual_bridge_world()
        task = asyncio.create_task(handler.resolve_move_action(
            "thread_a", labware["rack_a"],
            system_map.get_location("src_pad"),
            [system_map.get_location("dest")],
        ))
        try:
            await _pump(coordinator, ticks=10)
            assert task.done(), (
                "bridge A is completely free; the blocked sibling route's "
                "request for the shared terminal must not displace its grant"
            )
            move = task.result()
            assert move.target.position_id == "tA_in"
            active = {
                (position_id, thread_id)
                for position_id, _rid, thread_id in coordinator.get_active_reservations()
            }
            assert ("dest", "thread_a") in active, (
                "the crossing must keep the shared terminal grant"
            )
        finally:
            await _drain(task)


class TestKeptTerminalPrefersActionOwned:

    @pytest.mark.asyncio
    async def test_fresh_terminal_released_when_action_already_holds_one(self) -> None:
        """A crossing whose action already holds a terminal must not keep a
        fresh terminal grant beside it: the action hold covers the landing,
        and the extra grant starves sibling candidates for other threads."""
        _handler, _coordinator, system_map, labware = await _dual_bridge_world()
        rack = labware["rack_a"]
        move = MoveAction(
            rack,
            system_map.get_location("src_pad"),
            system_map.get_location("tA_in"),
            system_map.get_transporter_between("src_pad", "tA_in"),
            onward_seats=[system_map.get_location("tA_out")],
            terminal_candidates=[
                system_map.get_location("dest"),
                system_map.get_location("dest_2"),
            ],
        )
        action_res = LocationReservation(system_map.get_location("dest_2"), rack)
        move.substitute_onward_reservation("dest_2", action_res)
        move.reservation.granted.set()
        for r in move.onward_seat_reservations:
            r.granted.set()
        fresh_terminal = move.terminal_reservations[0]
        fresh_terminal.granted.set()
        action_res.granted.set()

        collection = MoveActionCollectionReservationRequest("thread_a", [move])
        collection.resolve_final_reservation()

        assert collection.granted.is_set()
        assert not fresh_terminal.granted.is_set(), (
            "the action-owned terminal covers the landing; the fresh grant "
            "is surplus and must be released"
        )
        assert action_res.granted.is_set(), (
            "the action hold is its own lifecycle, never released here"
        )


class TestCorridorHoldTracking:

    @pytest.mark.asyncio
    async def test_shared_holds_are_tracked_for_the_crossing(self) -> None:
        """A crowned route riding a sibling-owned (shared) hold must track it
        as a corridor hold; untracked, nothing ever sweeps or reconciles it."""
        handler, _coordinator, system_map, labware = await _dual_bridge_world()
        rack = labware["rack_a"]
        owner = MoveAction(
            rack,
            system_map.get_location("src_pad"),
            system_map.get_location("tB_in"),
            system_map.get_transporter_between("src_pad", "tB_in"),
            onward_seats=[system_map.get_location("tB_out")],
            terminal_candidates=[system_map.get_location("dest")],
        )
        borrower = MoveAction(
            rack,
            system_map.get_location("src_pad"),
            system_map.get_location("tA_in"),
            system_map.get_transporter_between("src_pad", "tA_in"),
            onward_seats=[system_map.get_location("tA_out")],
            terminal_candidates=[system_map.get_location("dest")],
        )
        MoveActionCollectionReservationRequest("thread_a", [owner, borrower])
        shared = borrower.shared_onward_reservations
        assert shared == [owner.terminal_reservations[0]], (
            "collection construction must collapse the shared terminal to "
            "the owner-move object"
        )
        for r in [borrower.reservation, *borrower.owned_onward_reservations, *shared]:
            r.granted.set()

        handler._record_corridor_holds("thread_a", borrower)
        held_ids = {r.id for r in handler._live_holds("thread_a")}
        assert shared[0].id in held_ids, (
            "the borrowed terminal hold is part of the crossing and must be "
            "tracked like an owned one"
        )

    @pytest.mark.asyncio
    async def test_dead_entries_pruned_on_record(self) -> None:
        """Corridor-hold entries whose grants all died (thread failed or
        aborted mid-route) are dropped the next time any thread records."""
        handler, _coordinator, system_map, labware = await _dual_bridge_world()
        rack = labware["rack_a"]
        dead = LocationReservation(system_map.get_location("dest_2"), rack)
        handler._corridor_holds["thread_gone"] = [dead]

        move = MoveAction(
            rack,
            system_map.get_location("src_pad"),
            system_map.get_location("tA_in"),
            system_map.get_transporter_between("src_pad", "tA_in"),
            onward_seats=[system_map.get_location("tA_out")],
            terminal_candidates=[system_map.get_location("dest")],
        )
        for r in [move.reservation, *move.owned_onward_reservations]:
            r.granted.set()
        handler._record_corridor_holds("thread_a", move)

        assert "thread_gone" not in handler._corridor_holds, (
            "an entry with no live holds is a zombie; recording must prune it"
        )
