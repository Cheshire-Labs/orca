"""A recording liquid handler over the simulator the built-in client gives a deck-modeling handler."""

from typing import ClassVar

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.gantry_models import ParkGantryRequest
from cheshire_drivers.interfaces import IGantryParkingDriver, ILiquidHandlerWithProtocolDriver
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from cheshire_drivers.protocol_runner_models import LabwareHandoffRequest, RunProtocolRequest
from cheshire_drivers.sims import RecordedCall


class RecordingHandler(RecordingLiquidHandlerDriver, ILiquidHandlerWithProtocolDriver, IGantryParkingDriver):
    """Records every call, as `RecordingLiquidHandlerDriver` does, and implements what its setup declares."""

    interfaces: ClassVar[frozenset[str]] = ChatterboxLiquidHandlerWithProtocolDriver.interfaces

    def __init__(self, inner: ChatterboxLiquidHandlerWithProtocolDriver | None = None) -> None:
        self._handler = inner or ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8)
        super().__init__(self._handler)

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        self.calls.append(RecordedCall(method="run_protocol", args=request.model_dump()))
        await self._handler.run_protocol(request)

    async def prepare_for_place(self, request: LabwareHandoffRequest) -> None:
        await self._handler.prepare_for_place(request)

    async def notify_placed(self, request: LabwareHandoffRequest) -> None:
        await self._handler.notify_placed(request)

    async def prepare_for_pick(self, request: LabwareHandoffRequest) -> None:
        await self._handler.prepare_for_pick(request)

    async def notify_picked(self, request: LabwareHandoffRequest) -> None:
        await self._handler.notify_picked(request)

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        await self._handler.park_gantry(request)
