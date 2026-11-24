"""A handler parks its gantry before another arm reaches into its deck.

A gantry stays wherever its last operation left it, with a gripper hanging off
it, and nothing else moves it. An arm coming in to that site meets it: on the
bench a PF400 placing a tip rack into a Flex staging slot clipped the Flex
gripper.
"""

import logging

import pytest
from cheshire_drivers.device_setup import DeviceSetup, RealDriverSetup, TcpConnection
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from cheshire_drivers.gantry_models import GantryParkPosition, ParkGantryRequest

from orca.devices.devices import GantryWillNotParkError, LiquidHandler
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode, mode_scope
from tests.device_modes import set_to

from tests.mock import outside_any_system
from tests.real_device_path import connected, the_real_device_path

pytestmark = pytest.mark.asyncio


class _RecordingHandlerDriver(ChatterboxLiquidHandlerWithProtocolDriver):
    """Records the parking calls, which is all these pin."""

    def __init__(self) -> None:
        super().__init__(num_channels=8)
        self.parked: list[GantryParkPosition | None] = []

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.parked.append(request.at)


class _Arm(IPlateMover):
    @property
    def name(self) -> str:
        return "pf400"

    @property
    def labware(self) -> LabwareInstance | None:
        return None

    @property
    def gripper_position_id(self) -> str:
        return "pf400/gripper"


async def _reach_in(handler: LiquidHandler) -> None:
    async with handler.reach_in_lease(_Arm(), "place"):
        return


async def _handler(park_at: GantryParkPosition | None) -> tuple[LiquidHandler, _RecordingHandlerDriver]:
    driver = _RecordingHandlerDriver()
    the_real_device_path().simulate_with("flex", driver)
    return await connected(outside_any_system(LiquidHandler("flex", park_gripper_at=park_at))), driver


async def test_a_handler_with_nowhere_declared_parks_itself() -> None:
    """The default: the handler is asked to move clear and picks where."""
    handler, driver = await _handler(None)

    await _reach_in(handler)

    assert driver.parked == [None]


async def test_a_declared_park_position_rides_on_the_park_request() -> None:
    """Bench geometry, declared in the topology: a short move beats a full home.

    It travels as part of the park rather than as a plain gripper jog, so the
    specific-spot case cannot skip the handler's refusal.
    """
    park = GantryParkPosition(x=20.0, y=20.0, z=200.0)
    handler, driver = await _handler(park)

    await _reach_in(handler)

    assert driver.parked == [park]


async def _handler_that_stays_put(park_at: GantryParkPosition | None = None) -> LiquidHandler:
    """What the bench actually had bound: a handler whose driver declares no park.

    The remote liquid-handler driver forwarded nothing that moved the gantry, so
    the capability probe missed and the park returned having done nothing.
    It read as wired for a whole bench session.
    """
    the_real_device_path().set_up("flex", DeviceSetup(
        client="lab_pc",
        real=RealDriverSetup(type="plr", backend="OpentronsOT2Backend", connection=TcpConnection(type="tcp", host="ot2")),
    ))
    return await connected(outside_any_system(LiquidHandler("flex", park_gripper_at=park_at)))


async def test_a_handler_that_can_neither_park_nor_home_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bench defect. Nothing was declared, so this is not a broken promise
    and does not raise -- but it must not read as though the gantry moved.

    A live run on a device set to live, as the bench was. In simulation the
    driver named is a simulator and the message carries no instruction.
    """
    handler = await _handler_that_stays_put()
    set_to(handler, handler.name, DeviceMode.LIVE)

    with caplog.at_level(logging.WARNING, logger="orca"):
        with mode_scope(WorkflowRunMode.LIVE):
            await _reach_in(handler)

    assert "cannot be moved off its own deck" in caplog.text
    assert "RemoteLiquidHandlerDriver" in caplog.text


# The harness builds a case's devices for the run mode it is parametrized with.
@pytest.mark.parametrize("mode", [WorkflowRunMode.CHECK_RUN, WorkflowRunMode.SIM])
async def test_a_simulated_run_reports_it_at_info_with_no_advice(
    caplog: pytest.LogCaptureFixture, mode: WorkflowRunMode,
) -> None:
    """The driver that cannot park there is a simulator nobody chose, so "bind a driver" is advice nobody can take."""
    handler = await _handler_that_stays_put()

    with caplog.at_level(logging.INFO, logger="orca"):
        with mode_scope(mode):
            await _reach_in(handler)

    assert "nothing parks a gantry in simulation" in caplog.text
    assert "Bind a driver" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_sim_run_does_not_use_up_the_warning_a_live_run_is_owed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The message is latched per device so a run does not repeat it every hop.

    Latching on "already said something" would let a SIM pass swallow the
    warning a later LIVE submission in the same process has to get.
    """
    handler = await _handler_that_stays_put()
    set_to(handler, handler.name, DeviceMode.LIVE)

    with caplog.at_level(logging.INFO, logger="orca"):
        with mode_scope(WorkflowRunMode.SIM):
            await _reach_in(handler)
    assert "nothing parks a gantry in simulation" in caplog.text
    # Whatever the sim pass said is not what this asserts, and it lands in
    # caplog whether or not the block below raised the level.
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="orca"):
        with mode_scope(WorkflowRunMode.LIVE):
            await _reach_in(handler)

    assert "cannot be moved off its own deck" in caplog.text


async def test_a_declared_park_the_driver_cannot_honor_refuses() -> None:
    """Declaring a park position the bound driver cannot reach is a topology
    error, and quietly staying put instead would hide it."""
    handler = await _handler_that_stays_put(GantryParkPosition(x=20.0, y=20.0, z=200.0))

    with pytest.raises(GantryWillNotParkError, match="cannot park"):
        await _reach_in(handler)
