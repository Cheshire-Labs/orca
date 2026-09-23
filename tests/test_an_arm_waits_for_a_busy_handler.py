"""An arm reaching into a handler's deck waits for the handler to move off it.

The step-aside after a handoff is an early park, not a guarantee: a gantry can be
over a shared site for reasons that have nothing to do with a handoff, and it is
somewhere unknown before the first handoff of a run. So the arm asks again on the
way in, and that is the ask that can wait.

Waiting is possible only on the way in. The device lock is taken per driver call
rather than per action, and a device reservation sanctions labware arriving and
leaving mid-action, so a handler really can be part-way through a method when an
arm arrives. It refuses while it holds tips; the transfer wins and the arm waits.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cheshire_drivers.gantry_models import (
    GantryBusyError,
    GantryParkPosition,
    ParkGantryRequest,
)

from cheshire_drivers.driver_errors import DriverRefusedError, InstrumentOutcome

from orca.devices import devices as devices_module
from orca.gateway.controller.controller import DeviceController
from orca.gateway.controller.exceptions import CommandExecutionError
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.devices.devices import CannotStepAsideError, LiquidHandler
from orca.resource_models.deck_access import clear_the_deck_for
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode

from tests.test_helpers import _SingleDriverFactory

pytestmark = pytest.mark.asyncio


class _HandlerBusyForNCalls:
    """Refuses the first ``busy_for`` asks, exactly as a handler holding tips does."""

    def __init__(self, busy_for: int) -> None:
        self._busy_for = busy_for
        self.asks = 0
        self.parked = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1
        if self.asks <= self._busy_for:
            raise GantryBusyError("FlexHead8 still holds tips")
        self.parked += 1


class _RemoteHandlerBusyForever:
    """A driver class does not survive the gateway, so the remote form of the
    refusal is a CommandExecutionError carrying what the driver said."""

    def __init__(self) -> None:
        self.asks = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1
        raise CommandExecutionError(
            "still holds tips", error_type="GantryBusyError",
            instrument_outcome=InstrumentOutcome.REFUSED,
        )


class _RecordingHandler:
    """Records where it was asked to park, which is all one test needs."""

    def __init__(self) -> None:
        self.asked_for: list[GantryParkPosition | None] = []

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asked_for.append(request.at)


class _HandlerThatBreaks:
    async def park_gantry(self, request: ParkGantryRequest) -> None:
        raise RuntimeError("the gantry motor faulted")


class _HandlerThatCannotDoThis:
    """The device bridge could not act on the command at all: it does not hold
    this device, does not have this command, or could not read the payload."""

    def __init__(self) -> None:
        self.asks = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1
        raise CommandExecutionError(
            "no handler named flex is connected",
            error_type="DeviceNotFoundError",
            instrument_outcome=InstrumentOutcome.REJECTED,
        )


class _Arm(IPlateMover):
    """Identity is all a placement hook needs from a mover."""

    def __init__(self, name: str = "pf400") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def labware(self) -> LabwareInstance | None:
        return None

    @property
    def gripper_position_id(self) -> str:
        return f"{self._name}/gripper"


def _handler(driver: Any, park_at: GantryParkPosition | None = None) -> LiquidHandler:
    with use_device_factory(_SingleDriverFactory(driver)):
        return LiquidHandler("flex", park_gripper_at=park_at)


@pytest.fixture(autouse=True)
def _dont_actually_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry cadence is wall-clock; these pin the waiting, not the clock."""
    monkeypatch.setattr(devices_module, "_STEP_ASIDE_RETRY_SECONDS", 0.0)


async def test_an_arm_waits_until_the_handler_is_done_and_then_goes_in() -> None:
    driver = _HandlerBusyForNCalls(busy_for=3)
    handler = _handler(driver)

    await handler.step_aside_for(_Arm())

    assert driver.asks == 4, "asked again each time it was refused"
    assert driver.parked == 1


async def test_a_command_the_agent_cannot_act_on_is_not_waited_on() -> None:
    """A rejection moved nothing, exactly like a refusal, and that is where the
    resemblance ends: no amount of waiting makes it work.

    Waiting on one costs the whole patience budget, 450 dispatches over 15
    minutes, and ends in an error saying the handler is holding tips, which
    nothing here established.

    This is the control on which question the wait asks. A "moved nothing"
    check reads true here and would wait; only "worth asking again" does not.
    """
    driver = _HandlerThatCannotDoThis()
    handler = _handler(driver)

    with pytest.raises(CommandExecutionError):
        await handler.step_aside_for(_Arm())

    assert driver.asks == 1, "asked once and gave up, rather than waiting it out"


async def test_a_refusal_from_a_driver_that_is_not_the_gantry_is_still_waited_on() -> None:
    """The wait is on what the driver declared, not on one class name. A second
    driver that means "not now" needs no second case in orca."""

    class _SomeOtherBusyDriver:
        def __init__(self) -> None:
            self.asks = 0
            self.parked = 0

        async def park_gantry(self, request: ParkGantryRequest) -> None:
            self.asks += 1
            if self.asks <= 2:
                raise DriverRefusedError("lid handler is mid-cycle")
            self.parked += 1

    driver = _SomeOtherBusyDriver()
    handler = _handler(driver)

    await handler.step_aside_for(_Arm())

    assert driver.asks == 3, "asked again each time it was refused"
    assert driver.parked == 1


async def test_a_handler_that_never_frees_up_stops_the_move_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A method that picks up tips and ends without dropping them leaves the
    handler permanently unwilling. An arm waiting forever is worse than an error
    naming what an operator has to do."""
    monkeypatch.setattr(devices_module, "STEP_ASIDE_PATIENCE_SECONDS", 0.0)
    driver = _HandlerBusyForNCalls(busy_for=99)
    handler = _handler(driver)

    with pytest.raises(CannotStepAsideError, match="mid-transfer"):
        await handler.step_aside_for(_Arm())


async def test_the_refusal_is_recognised_coming_back_over_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the bench the driver is remote. If the refusal is not recognised there
    it surfaces as a failed move rather than a wait, which is the whole point."""
    monkeypatch.setattr(devices_module, "STEP_ASIDE_PATIENCE_SECONDS", 0.0)
    driver = _RemoteHandlerBusyForever()
    handler = _handler(driver)

    with pytest.raises(CannotStepAsideError):
        await handler.step_aside_for(_Arm())


async def test_a_real_fault_is_not_mistaken_for_being_busy() -> None:
    """Waiting out a broken gantry would turn a fault into a silent stall."""
    handler = _handler(_HandlerThatBreaks())

    with pytest.raises(RuntimeError, match="motor faulted"):
        await handler.step_aside_for(_Arm())


async def test_the_handlers_own_gripper_is_not_asked_to_get_out_of_its_own_way() -> None:
    """An internal hop never leaves the deck, so parking between hops would add a
    gantry move to every relay and clear space for nobody."""
    driver = _HandlerBusyForNCalls(busy_for=0)
    handler = _handler(driver)
    own_gripper = _Arm("flex/gripper")
    handler.set_gripper(own_gripper)

    await handler.step_aside_for(own_gripper)

    assert driver.asks == 0


async def test_a_declared_spot_is_carried_on_the_way_in_too() -> None:
    driver = _RecordingHandler()
    park = GantryParkPosition(x=20.0, y=20.0, z=200.0)
    handler = _handler(driver, park)

    await handler.step_aside_for(_Arm())

    assert driver.asked_for == [park]


async def test_a_plain_pad_needs_no_clearing() -> None:
    """Most locations have nothing of their own moving over them."""
    await clear_the_deck_for(Location("pad_1", PlatePad("pad_1")), _Arm())


async def test_a_deck_site_clears_the_handler_that_owns_it() -> None:
    """The arm reaches a SITE; the thing that has to move belongs to the device
    behind it, which is what the location walk resolves."""
    driver = _HandlerBusyForNCalls(busy_for=1)
    handler = _handler(driver)
    site = Location("flex/B4-slot", DeviceDeckSite("flex/B4-slot", handler))

    await clear_the_deck_for(site, _Arm())

    assert driver.parked == 1


async def test_an_early_park_refused_for_being_busy_is_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The early park after a handoff cannot wait -- it runs under the device
    lock the busy transfer needs. Refusing there just defers to the arm's gate."""
    driver = _HandlerBusyForNCalls(busy_for=99)
    handler = _handler(driver)

    with caplog.at_level(logging.DEBUG, logger="orca"):
        await handler._step_aside()

    assert driver.asks == 1, "no waiting here"
    assert "stayed put" in caplog.text


async def test_the_transfer_the_arm_is_waiting_for_can_still_run() -> None:
    """The arm is waiting for tips to come off, and taking them off is another
    device call needing the same lock. Holding it across the wait would deadlock
    the arm against the very thing that would release it."""
    transfer_finished = asyncio.Event()

    class _BusyUntilTheTransferEnds:
        async def park_gantry(self, request: ParkGantryRequest) -> None:
            if not transfer_finished.is_set():
                raise GantryBusyError("FlexHead8 still holds tips")

    handler = _handler(_BusyUntilTheTransferEnds())

    async def _the_next_command_of_the_transfer() -> None:
        async with handler.lock.held_for("aspirate"):
            await asyncio.sleep(0)
            transfer_finished.set()

    await asyncio.wait_for(
        asyncio.gather(
            handler.step_aside_for(_Arm()), _the_next_command_of_the_transfer(),
        ),
        timeout=2,
    )


class _RemoteHandlerWithNoGantryToPark:
    """A remote driver carries the whole wire surface whatever it is bound to, so
    it always has the method. Only the advertised set says what the device does."""

    declared_interfaces = frozenset({"ILiquidHandler"})

    def __init__(self) -> None:
        self.asks = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1


async def test_a_remote_handler_that_advertises_no_parking_is_not_asked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structurally it looks able, so asking anyway would fail every handoff into
    that deck on a deployment that simply has nothing to park."""
    driver = _RemoteHandlerWithNoGantryToPark()
    handler = _handler(driver)

    with caplog.at_level(logging.WARNING, logger="orca"):
        await handler.step_aside_for(_Arm())

    assert driver.asks == 0
    assert "cannot be moved off its own deck" in caplog.text


async def test_a_handler_that_has_not_reported_in_yet_is_still_asked() -> None:
    """None is cold start, not a denial; refusing to ask would skip the park on
    the first move after a restart, which is exactly when it matters."""

    class _NotReportedIn(_HandlerBusyForNCalls):
        declared_interfaces = None

    driver = _NotReportedIn(busy_for=0)
    handler = _handler(driver)

    await handler.step_aside_for(_Arm())

    assert driver.parked == 1


async def test_the_refusal_does_not_name_a_cause_it_cannot_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler refuses to travel for two reasons and the message named one.

    The jaws case: a plate clamped in a gripper stops a park the same way
    tips on a head do. Telling an operator to discard tips they may not have
    sends them looking in the wrong place, while the driver's own text, which
    does say which, sat in parentheses behind it.
    """
    monkeypatch.setattr(devices_module, "STEP_ASIDE_PATIENCE_SECONDS", 0.0)
    driver = _HandlerBusyForNCalls(busy_for=99)
    handler = _handler(driver)

    with pytest.raises(CannotStepAsideError) as refused:
        await handler.step_aside_for(_Arm())

    message = str(refused.value)
    assert "discard them" not in message
    assert "a plate in its jaws" in message
    assert "Its own refusal below says which" in message


class _HandlerBehindTheGateway:
    """A handler whose park goes over the wire through a real DeviceController.

    Every device on a bench is reached this way, and the controller is where a
    failed command latches its device fault.
    """

    def __init__(self, controller: DeviceController) -> None:
        self._controller = controller
        self.asks = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1
        await self._controller.execute_command(
            device_id="flex", command="park_gantry", params={},
            timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
            execution_id="exec-1",
        )


def _controller_refusing_parks(busy_for: int) -> DeviceController:
    """A controller whose dispatch answers in place: the first ``busy_for``
    parks are refused the way a handler holding tips refuses, everything else
    succeeds."""
    controller = DeviceController()
    snapshot = DeviceSnapshot(
        type="liquid_handler", name="flex", interfaces=["ILiquidHandlerDriver"],
        capabilities=[], provides_state=False, methods={}, site="test", lab="test",
        workcell=None, status="ready", last_seen=datetime.now(timezone.utc),
    )
    parks = 0

    async def fake_dispatch(
        device_id: str, command_id: str, command: str, *args: Any, **kwargs: Any,
    ) -> None:
        nonlocal parks
        future = controller._command_futures[command_id]
        if command == "park_gantry":
            parks += 1
            if parks <= busy_for:
                future.set_exception(CommandExecutionError(
                    "park_gantry: FlexHead8 still holds tips, so a transfer is "
                    "under way. Ask again once the tips are off.",
                    "GantryBusyError",
                    InstrumentOutcome.REFUSED,
                ))
                return
        future.set_result(None)

    setattr(controller, "_validate_command", AsyncMock(return_value=snapshot))
    setattr(controller, "_dispatch_command", fake_dispatch)
    return controller


async def test_a_waited_out_refusal_leaves_the_handler_drivable() -> None:
    """The bench cascade, end to end.

    The arm's step-aside was refused while the handler was mid-transfer. The
    refusal latched a device fault, and the transfer's own next dispense -- on
    a different thread, on a handler that was working fine -- was then refused
    by that fault. Four threads paused holding aspirated sample.
    """
    controller = _controller_refusing_parks(busy_for=2)
    driver = _HandlerBehindTheGateway(controller)
    handler = _handler(driver)

    await handler.step_aside_for(_Arm())

    assert driver.asks == 3, "asked again each time it was refused"
    assert controller.fault("flex") is None

    dispense = await controller.execute_command(
        device_id="flex", command="dispense", params={},
        timeout_seconds=5.0, effective_mode=WorkflowRunMode.LIVE,
        execution_id="exec-2",
    )
    assert dispense is None
