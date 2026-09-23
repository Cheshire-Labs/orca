"""Contract tests for @orca.action -> DeviceHandle -> Device -> Driver parameter chains.

Verifies that parameters flow from the action through the DeviceHandle
to the device and driver with the correct semantic meaning.
"""

from dataclasses import dataclass
from typing import Any, Dict, List

import pytest

from cheshire_drivers import (
    SimShakerDriver,
)
from orca.devices.shaker import Shaker
from orca.resource_models.devices import Device
from orca.resource_models.location import Location
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.device_factory_context import use_device_factory
from orca.system.reservation_manager.location_reservation import LocationReservation


# ---------------------------------------------------------------------------
# Recording driver (test-local subclass that records `shake` calls)
# ---------------------------------------------------------------------------

@dataclass
class DriverCall:
    method: str
    kwargs: Dict[str, Any]


class _RecordingShaker(SimShakerDriver):
    def __init__(self) -> None:
        super().__init__("recording_shaker")
        self.calls: List[DriverCall] = []

    async def shake(self, request) -> None:  # request: ShakeRequest
        self.calls.append(
            DriverCall("shake", {"speed": request.speed, "duration": request.duration})
        )


class _RecordingShakerFactory:
    """Inject a specific shaker driver instance via the no-driver Device ctor."""

    def __init__(self, driver: _RecordingShaker) -> None:
        self._d = driver

    def build_drivers(
        self, device_type: str, name: str,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        return self._d, self._d


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _wire_action_to_device(action: Any, device: Device) -> None:
    """Wire a LocationAction to a device via a minimal reservation chain."""
    location = Location("test_location", resource=device)
    reservation = LocationReservation(requested_location=location)
    reservation.set_location(location)
    action.set_location_reservation(reservation)
    action.set_device(device)


# ---------------------------------------------------------------------------
# Contract test
# ---------------------------------------------------------------------------

class TestCodeFirstActionContract:
    """Verify @orca.action -> DeviceHandle -> device method parameter chain."""

    @pytest.mark.asyncio
    async def test_ctx_device_shake_reaches_driver(self) -> None:
        """ctx.device().shake(duration=30, speed=500) reaches the driver correctly."""
        driver = _RecordingShaker()
        with use_device_factory(_RecordingShakerFactory(driver)):
            shaker = Shaker("test_shaker")

        async def user_func(ctx: "ActionContext") -> None:
            await ctx.device().shake(duration=30, speed=500)

        from orca.workflow_models.actions.location_action import ActionBodyLocationAction
        from orca.workflow_models.action_context import ActionContext

        action = ActionBodyLocationAction(func=user_func, command="shake")
        action.set_execution_context(variable_store=None, execution_id="test", thread_id="test-thread")
        _wire_action_to_device(action, shaker)
        await action.execute()

        assert len(driver.calls) == 1
        call = driver.calls[0]
        assert call.kwargs["speed"] == 500
        assert call.kwargs["duration"] == 30