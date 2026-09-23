"""A handler moves its gantry off a site it has just handed a labware to.

A gantry stays wherever its last operation left it, with a gripper hanging off
it, and nothing else moves it. An arm coming in to that site meets it: on the
bench a PF400 placing a tip rack into a Flex staging slot clipped the Flex
gripper.
"""

import logging
from typing import Any

import pytest
from cheshire_drivers.gantry_models import GantryParkPosition, ParkGantryRequest

from orca.devices.devices import CannotStepAsideError, LiquidHandler
from orca.runtime.device_factory_context import use_device_factory

from tests.test_helpers import _SingleDriverFactory

pytestmark = pytest.mark.asyncio


class _RecordingHandlerDriver:
    """Records the parking calls, which is all these pin."""

    def __init__(self) -> None:
        self.parked: list[GantryParkPosition | None] = []

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        self.parked.append(request.at)


def _handler(park_at: GantryParkPosition | None) -> tuple[LiquidHandler, Any]:
    driver = _RecordingHandlerDriver()
    with use_device_factory(_SingleDriverFactory(driver)):
        return LiquidHandler("flex", park_gripper_at=park_at), driver


async def test_a_handler_with_nowhere_declared_parks_itself() -> None:
    """The default: the handler is asked to move clear and picks where."""
    handler, driver = _handler(None)

    await handler._step_aside()

    assert driver.parked == [None]


async def test_a_declared_park_position_rides_on_the_park_request() -> None:
    """Bench geometry, declared in the topology: a short move beats a full home.

    It travels as part of the park rather than as a plain gripper jog, so the
    specific-spot case cannot skip the handler's refusal.
    """
    park = GantryParkPosition(x=20.0, y=20.0, z=200.0)
    handler, driver = _handler(park)

    await handler._step_aside()

    assert driver.parked == [park]


class _DriverThatStaysPut:
    """What the bench actually had bound: a handler driver with no park verb.

    The remote liquid-handler driver forwarded nothing that moved the gantry, so
    the capability probe missed and the step-aside returned having done nothing.
    It read as wired for a whole bench session.
    """


def _handler_with(driver: Any, park_at: GantryParkPosition | None = None) -> LiquidHandler:
    with use_device_factory(_SingleDriverFactory(driver)):
        return LiquidHandler("flex", park_gripper_at=park_at)


async def test_a_handler_that_can_neither_park_nor_home_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bench defect. Nothing was declared, so this is not a broken promise
    and does not raise -- but it must not read as though the gantry moved."""
    handler = _handler_with(_DriverThatStaysPut())

    with caplog.at_level(logging.WARNING, logger="orca"):
        await handler._step_aside()

    assert "cannot be moved off its own deck" in caplog.text
    assert "_DriverThatStaysPut" in caplog.text


async def test_a_declared_park_the_driver_cannot_honor_refuses() -> None:
    """Declaring a park position the bound driver cannot reach is a topology
    error, and quietly staying put instead would hide it."""
    handler = _handler_with(
        _DriverThatStaysPut(), GantryParkPosition(x=20.0, y=20.0, z=200.0),
    )

    with pytest.raises(CannotStepAsideError, match="cannot park"):
        await handler._step_aside()
