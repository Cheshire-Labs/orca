"""A gateway-bound liquid handler can be told to move its gripper.

The verb existed everywhere except here: on the driver interface, in the
on-prem executor's request registry, and on the Opentrons Flex driver. The
remote driver never forwarded it, so orca's step-aside probe found no
``move_gripper_to`` on the bound driver, skipped, and left the Flex gantry
sitting over the slot the PF400 was about to reach into.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, cast

import pytest
from cheshire_drivers.gantry_models import ParkGantryRequest
from cheshire_drivers.gripper_models import MoveGripperToRequest

from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_drivers import (
    GantryParkingNotSupportedError,
    GripperMotionNotSupportedError,
    RemoteLiquidHandlerDriver,
)
from orca.runtime.run_modes import WorkflowRunMode

pytestmark = pytest.mark.asyncio

FLEX = frozenset(
    {"ILiquidHandler", "IPipetteMotion", "IGripperMotion", "IForceGripperJaw", "IGantryParking"}
)
NO_GRIPPER = frozenset({"ILiquidHandler", "IPipetteMotion"})


@dataclass
class _FakeController:
    calls: List[Dict[str, Any]] = field(default_factory=list)

    async def execute_command(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {}


def _driver(
    declared: frozenset[str] | None,
) -> tuple[RemoteLiquidHandlerDriver, _FakeController]:
    controller = _FakeController()
    driver = RemoteLiquidHandlerDriver(
        "flex_1",
        cast(DeviceController, controller),
        lambda _name: WorkflowRunMode.LIVE,
        declared_interfaces=declared,
    )
    return driver, controller


async def test_a_gripper_move_reaches_the_wire() -> None:
    driver, controller = _driver(FLEX)

    await driver.move_gripper_to(MoveGripperToRequest(x=20.0, y=30.0, z=200.0))

    assert [c["command"] for c in controller.calls] == ["move_gripper_to"]
    assert controller.calls[0]["params"] == {"x": 20.0, "y": 30.0, "z": 200.0}


async def test_a_handler_with_no_gripper_refuses_before_the_wire() -> None:
    """The wire surface is uniform across LH profiles, so without this gate
    every remote handler would answer a structural "can you move your gripper"
    probe and a gripperless one would only fail at the device."""
    driver, controller = _driver(NO_GRIPPER)

    with pytest.raises(GripperMotionNotSupportedError, match="no IGripperMotion"):
        await driver.move_gripper_to(MoveGripperToRequest(x=20.0, y=30.0, z=200.0))

    assert controller.calls == []


async def test_a_cold_start_driver_still_forwards() -> None:
    """Before any device bridge has advertised a card there is nothing to gate
    on, and the device bridge rejects what it cannot do."""
    driver, controller = _driver(None)

    await driver.move_gripper_to(MoveGripperToRequest(x=20.0, y=30.0, z=200.0))

    assert [c["command"] for c in controller.calls] == ["move_gripper_to"]


async def test_a_park_reaches_the_wire() -> None:
    """The default step-aside: the handler is told to move clear and picks where."""
    driver, controller = _driver(FLEX)

    await driver.park_gantry(ParkGantryRequest())

    assert [c["command"] for c in controller.calls] == ["park_gantry"]
    assert controller.calls[0]["params"] == {}


async def test_a_handler_that_cannot_park_refuses_before_the_wire() -> None:
    driver, controller = _driver(NO_GRIPPER)

    with pytest.raises(GantryParkingNotSupportedError, match="no IGantryParking"):
        await driver.park_gantry(ParkGantryRequest())

    assert controller.calls == []
