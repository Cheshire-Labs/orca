"""A check run's orca-client, inside Orca.

It builds each device's simulator and runs each command with the same cheshire-drivers
code orca-client uses, behind the same `DeviceController`, so a check run behaves as a
sim run does with no network and no pacing. A simulator this server cannot
build (a vendor simulator) is recorded as that device's build error, and the run is
refused for it as a sim run would be.
"""

import asyncio
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Callable, List

from cheshire_drivers.command_executor import CommandExecutor, DeviceDrivers, DeviceRegistry, answer_command
from cheshire_drivers.driver_lookup import Driver, DriverRequest, build_sim_driver
from cheshire_drivers.gateway_protocol import (
    AssignedDevice,
    CancelMessage,
    CommandMessage,
    DriverMode,
    EffectiveMode,
    MessageEnvelope,
)

from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.registry.connection_tracker import IDeviceConnectionTracker
from orca.gateway.websocket.connection_events import take_status_report

logger = logging.getLogger(__name__)

CHECK_RUN_CLIENT_ID = "check-run"

# Well inside the tracker's heartbeat tolerance.
_HEARTBEAT_SECONDS = 10.0

CHECK_RUN_ON_THE_SIMULATOR: Mapping[EffectiveMode, DriverMode] = MappingProxyType({"CHECK_RUN": "SIM"})
"""A check run's command runs on the device's simulator. A SIM or LIVE command has no driver here."""


class InProcessClient:
    """Runs every device's simulator in this process, as one orca-client would over the websocket."""

    def __init__(
        self, tracker: IDeviceConnectionTracker, simulators: Callable[[DriverRequest], Driver] = build_sim_driver,
        runs_on: Mapping[EffectiveMode, DriverMode] = CHECK_RUN_ON_THE_SIMULATOR,
    ) -> None:
        self._tracker = tracker
        self._simulators = simulators
        self._devices = DeviceRegistry(runs_on)
        self._executor = CommandExecutor(self._devices)
        self._gateway: DeviceGateway | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._heartbeat: asyncio.Task[None] | None = None

    def answer_to(self, gateway: DeviceGateway) -> None:
        """The gateway each response and status report goes back to."""
        self._gateway = gateway

    async def run(self, devices: List[AssignedDevice]) -> None:
        """Run exactly these devices' simulators, and report what was built, as orca-client answers an assignment."""
        errors = await self._devices.apply(devices, self._simulator_only)
        for name, refused in errors.items():
            await self._tracker.record_build_error(CHECK_RUN_CLIENT_ID, name, refused)
        await self._tracker.register_client(
            client_id=CHECK_RUN_CLIENT_ID, site="", lab="", workcell=None, devices=self._devices.connect_info_list(),
        )
        await self._report_status()
        if self._heartbeat is None:
            self._heartbeat = asyncio.create_task(self._beat())

    async def _simulator_only(self, device: AssignedDevice) -> DeviceDrivers:
        return DeviceDrivers(live=None, sim=self._simulators(DriverRequest.for_device(device)))

    async def _beat(self) -> None:
        while True:
            try:
                await self._tracker.update_heartbeat(CHECK_RUN_CLIENT_ID)
                await self._report_status()
            except Exception:
                logger.exception("The check run's heartbeat failed; trying again")
            await asyncio.sleep(_HEARTBEAT_SECONDS)

    async def _report_status(self) -> None:
        """What this client sees on each device, sent as orca-client sends it after each command and heartbeat."""
        await take_status_report(self._tracker, self._answering().events, CHECK_RUN_CLIENT_ID, self._devices.status_report())

    def _answering(self) -> DeviceGateway:
        if self._gateway is None:
            raise RuntimeError("the check run's client has no gateway to answer")
        return self._gateway

    async def aclose(self) -> None:
        """Stop the heartbeat, end every command still running, and release every simulator."""
        pending = [t for t in (self._heartbeat, *self._tasks) if t is not None]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self._devices.apply([], self._simulator_only)

    def simulator(self, device_name: str) -> Driver | None:
        """The simulator this client runs the device on, or None when it holds none."""
        return self._devices.get_sim_driver(device_name)

    async def send_to_client(self, client_id: str, message: str) -> bool:
        envelope = MessageEnvelope.model_validate_json(message)
        if envelope.type == "command":
            task = asyncio.create_task(self._answer(CommandMessage.model_validate(envelope.payload)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif envelope.type == "cancel":
            await self._executor.cancel(CancelMessage.model_validate(envelope.payload).command_id)
        return True

    def connected_client_ids(self) -> List[str]:
        # The check run hands this client its assignment directly, so no client is listed.
        return []

    async def _answer(self, command: CommandMessage) -> None:
        await answer_command(
            self._executor, command,
            respond=self._answering().controller.handle_response, report_status=self._report_status,
        )
