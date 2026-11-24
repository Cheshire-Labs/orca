"""An arm reaching into a device holds that device's lock, on every route.

A run's move and an operator's own pick or place both reach a driver's pick and
place only through the transporter, which holds the lock of the device whose
site the arm reaches into. The site's hooks run on both routes, so a reader's
tray is open before the arm goes in and closed after it comes out.
"""

from typing import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway.mode_doubles import unseeded_await
import pytest_asyncio
from cheshire_drivers.transporter_models import PickAtCoordsRequest
from pydantic import JsonValue

from orca.gateway import operator_commands
from tests.gateway._operator_command import run_operator_command
from orca.gateway.controller.exceptions import DeviceLockedError, InvalidCommandError
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.devices import Device
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import (
    OUT_OF_RUN_BASE,
    WorkflowRunMode,
    current_run_mode,
)
from tests.gateway_parts import gateway_parts, gateway_with
from tests.test_adhoc_commands_are_recorded import (
    _Bench,
    _teachpoint,
    _wire_to,
    running_arm_bench,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def arm_bench() -> AsyncIterator[_Bench]:
    async with running_arm_bench() as bench:
        yield bench


def _stacker(bench: _Bench) -> Device:
    return bench.runtime.system.get_device("stacker")


def _arm(bench: _Bench) -> Transporter:
    mover = next(m for m in bench.runtime.system.movers if m.name == "arm")
    assert isinstance(mover, Transporter)
    return mover


async def _put_the_plate_in_the_stacker(bench: _Bench) -> None:
    await bench.runtime.labware.edit_location(
        bench.labware_id, "stacker/slot", reason="moved by hand", confirm=True,
    )


def _record_the_tray(bench: _Bench, calls: list[str]) -> None:
    """The stacker's driver writes its tray moves to ``calls``."""
    for driver in [_stacker(bench).driver]:
        async def opened() -> None:
            calls.append("open")

        async def closed() -> None:
            calls.append("close")

        setattr(driver, "open", opened)
        setattr(driver, "close", closed)


def _the_arm_reports_its_moves_to(bench: _Bench, on_dispatch: Callable[[str], Awaitable[None]]) -> None:
    """An operator's pick or place reaches the arm through Transporter.pick and place, so watch the arm's own driver."""
    for driver in [_arm(bench).driver]:
        for command in ("pick_at_coords", "place_at_coords"):
            sent = getattr(driver, command)

            async def reported(request: PickAtCoordsRequest, sent=sent, command=command) -> None:
                await on_dispatch(command)
                await sent(request)

            setattr(driver, command, reported)


async def _operator(
    bench: _Bench, command: str, params: dict[str, JsonValue],
    on_dispatch: Callable[[str], Awaitable[None]] | None = None,
    *, device_id: str = "arm", interfaces: str = "ITransporter",
) -> None:
    """An operator's own command, through the route every surface uses, as a surface sends it: with no run mode set."""
    snapshot = MagicMock()
    snapshot.interfaces = [interfaces]
    wire = _wire_to(bench)

    async def execute_command(
        *, device_id: str, command: str, params: dict[str, JsonValue], **rest: object,
    ) -> JsonValue:
        if on_dispatch is not None:
            await on_dispatch(command)
        result: JsonValue = await wire(device_id=device_id, command=command, params=params)
        return result

    tracker = MagicMock()
    tracker.get_device = AsyncMock(return_value=snapshot)
    controller = MagicMock()
    controller.execute_command = AsyncMock(side_effect=execute_command)
    if on_dispatch is not None:
        _the_arm_reports_its_moves_to(bench, on_dispatch)
    await unseeded_await(run_operator_command(
        gateway_with(tracker=tracker, controller=controller),
        bench.runtime, device_id, command, params,
    ))


@pytest.mark.timeout(30)
async def test_an_operator_pick_from_a_device_opens_it_first_and_closes_it_after(
    arm_bench: _Bench,
) -> None:
    await _operator(arm_bench, "pick_at_coords", _teachpoint("pad"))
    await _operator(arm_bench, "place_at_coords", _teachpoint("stacker"))
    calls: list[str] = []
    _record_the_tray(arm_bench, calls)

    async def arm_moves(command: str) -> None:
        calls.append(command)

    await _operator(arm_bench, "pick_at_coords", _teachpoint("stacker"), arm_moves)

    assert calls == ["open", "pick_at_coords", "close"]


@pytest.mark.timeout(30)
async def test_an_operator_place_into_a_device_opens_it_first_and_closes_it_after(
    arm_bench: _Bench,
) -> None:
    await _operator(arm_bench, "pick_at_coords", _teachpoint("pad"))
    calls: list[str] = []
    _record_the_tray(arm_bench, calls)

    async def arm_moves(command: str) -> None:
        calls.append(command)

    await _operator(arm_bench, "place_at_coords", _teachpoint("stacker"), arm_moves)

    assert calls == ["open", "place_at_coords", "close"]


@pytest.mark.timeout(30)
async def test_an_operator_pick_holds_the_device_while_the_arm_is_inside(
    arm_bench: _Bench,
) -> None:
    await _put_the_plate_in_the_stacker(arm_bench)
    held_during_the_pick: list[bool] = []

    async def arm_moves(command: str) -> None:
        held_during_the_pick.append(_stacker(arm_bench).lock.locked())

    await _operator(arm_bench, "pick_at_coords", _teachpoint("stacker"), arm_moves)

    assert held_during_the_pick == [True]
    assert not _stacker(arm_bench).lock.locked()


@pytest.mark.timeout(30)
async def test_an_operator_pick_takes_the_arm_before_the_device_opens_for_it(
    arm_bench: _Bench,
) -> None:
    """Checked and taken with nothing awaited between, so the engine cannot take the arm while the tray opens."""
    await _operator(arm_bench, "pick_at_coords", _teachpoint("pad"))
    await _operator(arm_bench, "place_at_coords", _teachpoint("stacker"))
    arm_held_while_opening: list[bool] = []

    async def opened() -> None:
        arm_held_while_opening.append(_arm(arm_bench).lock.locked())

    setattr(_stacker(arm_bench).driver, "open", opened)

    await _operator(arm_bench, "pick_at_coords", _teachpoint("stacker"))

    assert arm_held_while_opening == [True]


@pytest.mark.timeout(30)
async def test_an_operator_pick_is_refused_while_the_engine_holds_the_device(
    arm_bench: _Bench,
) -> None:
    """Refused at once, not queued: the engine's call can be a two-hour shake."""
    await _put_the_plate_in_the_stacker(arm_bench)
    dispatched: list[str] = []

    async def arm_moves(command: str) -> None:
        dispatched.append(command)

    async with _stacker(arm_bench).lock.held_for("shake"):
        with pytest.raises(DeviceLockedError, match="shake"):
            await _operator(arm_bench, "pick_at_coords", _teachpoint("stacker"), arm_moves)

    assert dispatched == []


@pytest.mark.timeout(30)
async def test_an_operator_command_to_a_device_holds_its_lock(arm_bench: _Bench) -> None:
    """Every operator command takes its device's lock, so the engine cannot drive the device during it."""
    held_during_the_command: list[bool] = []

    async def device_moves(command: str) -> None:
        held_during_the_command.append(_stacker(arm_bench).lock.locked())

    await _operator(arm_bench, "lock_door", {}, device_moves, device_id="stacker", interfaces="IStorage")

    assert held_during_the_command == [True]


@pytest.mark.timeout(30)
async def test_an_operator_command_is_refused_while_the_engine_holds_its_device(
    arm_bench: _Bench,
) -> None:
    dispatched: list[str] = []

    async def device_moves(command: str) -> None:
        dispatched.append(command)

    async with _stacker(arm_bench).lock.held_for("shake"):
        with pytest.raises(DeviceLockedError, match="shake"):
            await _operator(
                arm_bench, "lock_door", {}, device_moves, device_id="stacker", interfaces="IStorage",
            )

    assert dispatched == []


@pytest.mark.timeout(30)
async def test_an_operator_reach_in_runs_in_the_operators_world(arm_bench: _Bench) -> None:
    """A surface sets no run mode. The lease parks a handler's gantry with a device call,
    which must go to the operator's world, not fall back to the check-run simulator."""
    await _put_the_plate_in_the_stacker(arm_bench)
    modes: list[WorkflowRunMode] = []

    async def arm_moves(command: str) -> None:
        modes.append(current_run_mode.get(OUT_OF_RUN_BASE))

    await _operator(arm_bench, "pick_at_coords", _teachpoint("stacker"), arm_moves)

    assert modes == [arm_bench.runtime.operator_base_mode()]
    assert modes != [WorkflowRunMode.CHECK_RUN]


@pytest.mark.timeout(30)
async def test_a_runs_pick_holds_the_device_while_the_arm_is_inside(
    arm_bench: _Bench,
) -> None:
    await _put_the_plate_in_the_stacker(arm_bench)
    stacker = _stacker(arm_bench)
    arm = _arm(arm_bench)
    held_during_the_pick: list[bool] = []

    for driver in [arm.driver]:
        async def pick_at_coords(request: PickAtCoordsRequest) -> None:
            held_during_the_pick.append(stacker.lock.locked())

        setattr(driver, "pick_at_coords", pick_at_coords)

    site = arm_bench.runtime.system.system_map.resolve_placement_location("stacker")
    await arm.pick(site)

    assert held_during_the_pick == [True]
    assert not stacker.lock.locked()


async def test_the_controller_refuses_an_arm_pick_that_is_not_a_reach_in() -> None:
    """A pick sent straight at the controller skips the device's hooks and its lock."""
    controller = gateway_parts().controller

    with pytest.raises(InvalidCommandError, match="reach"):
        await controller.execute_command(
            device_id="arm", command="pick_at_coords", params={},
            effective_mode=WorkflowRunMode.LIVE,
        )


@pytest.mark.timeout(30)
async def test_an_operator_pick_at_no_known_site_is_refused(arm_bench: _Bench) -> None:
    """A pick goes where a run's would, at a site, so a position no site stands behind has nothing to hold or follow."""
    dispatched: list[str] = []

    async def arm_moves(command: str) -> None:
        dispatched.append(command)

    with pytest.raises(operator_commands.ReachInAtNoKnownSiteError):
        await _operator(arm_bench, "pick_at_coords", _teachpoint("nowhere_known"), arm_moves)

    assert dispatched == []


@pytest.mark.timeout(30)
async def test_an_operator_place_into_a_device_the_record_says_is_full_is_refused_before_the_arm_moves(
    arm_bench: _Bench,
) -> None:
    """The device's own hook refuses: an arm placing into a full device hits what is there."""
    await _operator(arm_bench, "pick_at_coords", _teachpoint("pad"))
    snapshot = await arm_bench.runtime.labware.register(
        "sample_plate", location="stacker/slot", confirm=True,
    )
    assert snapshot.id
    dispatched: list[str] = []

    async def arm_moves(command: str) -> None:
        dispatched.append(command)

    with pytest.raises(SlotOccupiedError):
        await _operator(arm_bench, "place_at_coords", _teachpoint("stacker"), arm_moves)

    assert dispatched == []
