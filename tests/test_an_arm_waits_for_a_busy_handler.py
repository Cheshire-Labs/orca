"""An arm reaching into a handler's deck waits for the handler to move off it.

The reach-in lease parks the gantry before the arm goes in. A device reservation
sanctions labware arriving and leaving mid-action, so a handler can be part-way
through a method when an arm arrives. It refuses while it holds tips; the
transfer wins and the arm waits, with the lease released between asks.
"""

import asyncio
import logging

import pytest

from orca.modes.device_modes import DeviceMode
from tests.device_modes import set_to
from cheshire_drivers.gantry_models import (
    GantryBusyError,
    GantryParkPosition,
    ParkGantryRequest,
)

from cheshire_drivers.device_setup import DeviceSetup, RealDriverSetup, TcpConnection
from cheshire_drivers.driver_errors import DriverRefusedError, DriverRejectedError
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from cheshire_drivers.driver_lookup import Driver
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from cheshire_drivers.teachpoints import Teachpoint

from orca.devices import devices as devices_module
from orca.gateway.controller.exceptions import CommandExecutionError
from orca.devices.devices import GantryWillNotParkError, LiquidHandler
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter_base import TransporterBase
from orca.modes.run_modes import WorkflowRunMode, mode_scope

from orca.driver_management.device_factory_context import use_device_factory
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from tests.gateway.mode_doubles import fixed_mode
from tests.mock import outside_any_system
from tests.real_device_path import connected, the_real_device_path

pytestmark = pytest.mark.asyncio


class _Handler(ChatterboxLiquidHandlerWithProtocolDriver):
    """The simulator a check run gives this handler, with its park replaced."""

    def __init__(self) -> None:
        super().__init__(num_channels=8)


class _HandlerBusyForNCalls(_Handler):
    """Refuses the first ``busy_for`` asks, exactly as a handler holding tips does."""

    def __init__(self, busy_for: int) -> None:
        super().__init__()
        self._busy_for = busy_for
        self.asks = 0
        self.parked = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1
        if self.asks <= self._busy_for:
            raise GantryBusyError("FlexHead8 still holds tips")
        self.parked += 1


class _RecordingHandler(_Handler):
    """Records where it was asked to park, which is all one test needs."""

    def __init__(self) -> None:
        super().__init__()
        self.asked_for: list[GantryParkPosition | None] = []

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asked_for.append(request.at)


class _HandlerThatBreaks(_Handler):
    async def park_gantry(self, request: ParkGantryRequest) -> None:
        raise RuntimeError("the gantry motor faulted")


class _HandlerThatCannotDoThis(_Handler):
    """The device bridge could not act on the command at all: it does not hold
    this device, does not have this command, or could not read the payload."""

    def __init__(self) -> None:
        super().__init__()
        self.asks = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1
        raise DriverRejectedError("no handler named flex is connected")


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


class _ArmThatReaches(TransporterBase):
    """A transporter with nothing to actuate, for the site-to-device walk."""

    async def _do_pick(self, location: Location) -> None:
        return

    async def _do_place(self, location: Location) -> None:
        return

    async def initialize(self) -> None:
        return

    @property
    def is_initialized(self) -> bool:
        return True

    async def get_teachpoints(self) -> list[Teachpoint]:
        return []


async def _reach_in(handler: LiquidHandler, arm: IPlateMover) -> None:
    async with handler.reach_in_lease(arm, "place"):
        return


async def _handler(driver: Driver, park_at: GantryParkPosition | None = None) -> LiquidHandler:
    the_real_device_path().simulate_with("flex", driver)
    return await connected(outside_any_system(LiquidHandler("flex", park_gripper_at=park_at)))


@pytest.fixture(autouse=True)
def _dont_actually_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry cadence is wall-clock; these pin the waiting, not the clock."""
    monkeypatch.setattr(devices_module, "_REACH_IN_RETRY_SECONDS", 0.0)


async def test_an_arm_waits_until_the_handler_is_done_and_then_goes_in() -> None:
    driver = _HandlerBusyForNCalls(busy_for=3)
    handler = await _handler(driver)

    await _reach_in(handler, _Arm())

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
    handler = await _handler(driver)

    with pytest.raises(CommandExecutionError):
        await _reach_in(handler, _Arm())

    assert driver.asks == 1, "asked once and gave up, rather than waiting it out"


async def test_a_refusal_from_a_driver_that_is_not_the_gantry_is_still_waited_on() -> None:
    """The wait is on what the driver declared, not on one class name. A second
    driver that means "not now" needs no second case in orca."""

    class _SomeOtherBusyDriver(_Handler):
        def __init__(self) -> None:
            super().__init__()
            self.asks = 0
            self.parked = 0

        async def park_gantry(self, request: ParkGantryRequest) -> None:
            self.asks += 1
            if self.asks <= 2:
                raise DriverRefusedError("lid handler is mid-cycle")
            self.parked += 1

    driver = _SomeOtherBusyDriver()
    handler = await _handler(driver)

    await _reach_in(handler, _Arm())

    assert driver.asks == 3, "asked again each time it was refused"
    assert driver.parked == 1


async def test_a_handler_that_never_frees_up_stops_the_move_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A method that picks up tips and ends without dropping them leaves the
    handler permanently unwilling. An arm waiting forever is worse than an error
    naming what an operator has to do."""
    monkeypatch.setattr(devices_module, "REACH_IN_PATIENCE_SECONDS", 0.0)
    driver = _HandlerBusyForNCalls(busy_for=99)
    handler = await _handler(driver)

    with pytest.raises(GantryWillNotParkError, match="mid-transfer"):
        await _reach_in(handler, _Arm())


async def test_the_refusal_is_recognised_coming_back_over_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the bench the driver is remote. If the refusal is not recognised there
    it surfaces as a failed move rather than a wait, which is the whole point."""
    monkeypatch.setattr(devices_module, "REACH_IN_PATIENCE_SECONDS", 0.0)
    driver = _HandlerBusyForNCalls(busy_for=99)
    handler = await _handler(driver)

    with pytest.raises(GantryWillNotParkError):
        await _reach_in(handler, _Arm())


async def test_a_real_fault_is_not_mistaken_for_being_busy() -> None:
    """Waiting out a broken gantry would turn a fault into a silent stall."""
    handler = await _handler(_HandlerThatBreaks())

    with pytest.raises(CommandExecutionError, match="motor faulted"):
        await _reach_in(handler, _Arm())


async def test_the_handlers_own_gripper_is_not_asked_to_get_out_of_its_own_way() -> None:
    """An internal hop never leaves the deck, so parking between hops would add a
    gantry move to every relay and clear space for nobody."""
    driver = _HandlerBusyForNCalls(busy_for=0)
    handler = await _handler(driver)
    own_gripper = _Arm("flex/gripper")
    handler.set_gripper(own_gripper)

    await _reach_in(handler, own_gripper)

    assert driver.asks == 0


async def test_a_declared_spot_is_carried_on_the_way_in_too() -> None:
    driver = _RecordingHandler()
    park = GantryParkPosition(x=20.0, y=20.0, z=200.0)
    handler = await _handler(driver, park)

    await _reach_in(handler, _Arm())

    assert driver.asked_for == [park]


async def test_a_plain_pad_needs_no_clearing() -> None:
    """Most locations have nothing of their own moving over them."""
    await _ArmThatReaches("pf400").make_room_at(Location("pad_1", PlatePad("pad_1")))


async def test_a_deck_site_clears_the_handler_that_owns_it() -> None:
    """The arm reaches a SITE; the thing that has to move belongs to the device
    behind it, which is what the location walk resolves."""
    driver = _HandlerBusyForNCalls(busy_for=1)
    handler = await _handler(driver)
    site = Location("flex/B4-slot", DeviceDeckSite("flex/B4-slot", handler))

    await _ArmThatReaches("pf400").make_room_at(site)

    assert driver.parked == 1


async def test_the_gantry_stays_parked_while_the_arm_is_inside() -> None:
    """The lease holds the handler from the park until the arm is out, so no
    device call can move the gantry back over the site in between."""
    handler = await _handler(_HandlerBusyForNCalls(busy_for=0))

    async with handler.reach_in_lease(_Arm(), "place"):
        assert handler.lock.locked()
    assert not handler.lock.locked()


async def test_the_transfer_the_arm_is_waiting_for_can_still_run() -> None:
    """The arm is waiting for tips to come off, and taking them off is another
    device call needing the same lock. Holding it across the wait would deadlock
    the arm against the very thing that would release it."""
    transfer_finished = asyncio.Event()

    class _BusyUntilTheTransferEnds(_Handler):
        async def park_gantry(self, request: ParkGantryRequest) -> None:
            if not transfer_finished.is_set():
                raise GantryBusyError("FlexHead8 still holds tips")

    handler = await _handler(_BusyUntilTheTransferEnds())

    async def _the_next_command_of_the_transfer() -> None:
        async with handler.lock.held_for("aspirate"):
            await asyncio.sleep(0)
            transfer_finished.set()

    await asyncio.wait_for(
        asyncio.gather(
            _reach_in(handler, _Arm()), _the_next_command_of_the_transfer(),
        ),
        timeout=2,
    )


class _HandlerWithNoGantryToPark(_Handler):
    """The simulator still has the method; only the setup's declared set says what the device does."""

    def __init__(self) -> None:
        super().__init__()
        self.asks = 0

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.asks += 1


def _set_up_flex_as_an_ot2() -> None:
    """An OT-2 on a lab PC: its driver declares no gantry parking."""
    the_real_device_path().set_up("flex", DeviceSetup(
        client="lab_pc",
        real=RealDriverSetup(type="plr", backend="OpentronsOT2Backend", connection=TcpConnection(type="tcp", host="ot2")),
    ))


async def test_a_remote_handler_that_advertises_no_parking_is_not_asked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structurally it looks able, so asking anyway would fail every handoff into
    that deck on a deployment that simply has nothing to park.

    A live run on a handler set to live: only a live device gets the warning and its advice.
    """
    _set_up_flex_as_an_ot2()
    driver = _HandlerWithNoGantryToPark()
    handler = await _handler(driver)
    set_to(handler, handler.name, DeviceMode.LIVE)

    with caplog.at_level(logging.WARNING, logger="orca"):
        with mode_scope(WorkflowRunMode.LIVE):
            await _reach_in(handler, _Arm())

    assert driver.asks == 0
    assert "cannot be moved off its own deck" in caplog.text


async def test_a_handler_that_has_not_reported_in_yet_is_still_asked() -> None:
    """None is cold start, not a denial; refusing to ask would skip the park on
    the first move after a restart, which is exactly when it matters.

    A forwarding factory given no setup to read builds the driver with no declared interfaces.
    """
    path = the_real_device_path()
    driver = _HandlerBusyForNCalls(busy_for=0)
    path.simulate_with("flex", driver)
    no_setup = RemoteDeviceFactory(controller=path.gateway.controller, mode_resolver=fixed_mode(WorkflowRunMode.CHECK_RUN))
    with use_device_factory(no_setup):
        handler = await connected(outside_any_system(LiquidHandler("flex")))

    await _reach_in(handler, _Arm())

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
    monkeypatch.setattr(devices_module, "REACH_IN_PATIENCE_SECONDS", 0.0)
    driver = _HandlerBusyForNCalls(busy_for=99)
    handler = await _handler(driver)

    with pytest.raises(GantryWillNotParkError) as refused:
        await _reach_in(handler, _Arm())

    message = str(refused.value)
    assert "discard them" not in message
    assert "a plate in its jaws" in message
    assert "Its own refusal below says which" in message


async def test_a_waited_out_refusal_leaves_the_handler_drivable() -> None:
    """The bench cascade, end to end.

    The arm's park was refused while the handler was mid-transfer. The
    refusal latched a device fault, and the transfer's own next dispense -- on
    a different thread, on a handler that was working fine -- was then refused
    by that fault. Four threads paused holding aspirated sample.
    """
    driver = _HandlerBusyForNCalls(busy_for=2)
    handler = await _handler(driver)

    await _reach_in(handler, _Arm())

    assert driver.asks == 3, "asked again each time it was refused"
    assert the_real_device_path().gateway.controller.fault("flex") is None
    await handler.driver.get_deck_state(GetDeckStateRequest())
