"""The devices each orca-client must run, from the last successful build."""

import asyncio
import logging
import uuid
from typing import Protocol

from cheshire_drivers.gateway_protocol import AssignedDevice, DeviceAssignment, MessageEnvelope

from orca.gateway.websocket.manager import IClientChannel

logger = logging.getLogger(__name__)


class IDeviceAssignments(Protocol):
    """The devices each orca-client must run, sent on connect and after each build."""

    def for_client(self, client_id: str) -> DeviceAssignment | None: ...

    async def publish(self, devices: list[AssignedDevice]) -> None: ...

    async def send_to(self, client_id: str) -> None: ...

    def is_latest(self, assignment_id: str) -> bool: ...

    def record_build(self, client_id: str, assignment_id: str, *, applied: bool) -> None: ...

    def changed_since_last_build(self, client_id: str) -> set[str]: ...

    async def wait_for_build(self, client_id: str, timeout: float) -> bool: ...

    async def wait_for_every_sent_build(self, timeout: float) -> list[str]: ...


class DeviceAssignments:
    """A snapshot, so a reconnect gets what the running system was built from and not a later hand edit."""

    def __init__(self, connections: IClientChannel) -> None:
        self._connections = connections
        self._devices: list[AssignedDevice] | None = None
        self._assignment_id: str | None = None
        self._reported: dict[str, asyncio.Event] = {}
        self._sent_devices_to: set[str] = set()
        self._built: dict[str, dict[str, AssignedDevice]] = {}

    def is_latest(self, assignment_id: str) -> bool:
        return assignment_id == self._assignment_id

    def record_build(self, client_id: str, assignment_id: str, *, applied: bool) -> None:
        """The client reported on an assignment. One it could not apply left its devices as they were built before."""
        if not self.is_latest(assignment_id):
            return
        self._reported.setdefault(client_id, asyncio.Event()).set()
        if applied:
            self._built[client_id] = self._latest_for(client_id)

    def changed_since_last_build(self, client_id: str) -> set[str]:
        """Devices whose setup in the latest assignment differs from the one the client last built: it rebuilds them."""
        built = self._built.get(client_id, {})
        return {name for name, device in self._latest_for(client_id).items() if built.get(name) != device}

    def _latest_for(self, client_id: str) -> dict[str, AssignedDevice]:
        assignment = self.for_client(client_id)
        return {} if assignment is None else {device.name: device for device in assignment.devices}

    async def wait_for_build(self, client_id: str, timeout: float) -> bool:
        """True once the client has reported on the latest assignment, or at once when it assigns the client nothing."""
        assignment = self.for_client(client_id)
        if assignment is None or not assignment.devices:
            return True
        try:
            await asyncio.wait_for(self._reported.setdefault(client_id, asyncio.Event()).wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def wait_for_every_sent_build(self, timeout: float) -> list[str]:
        """Wait for every client the latest assignment sent devices to. Returns the ones that did not report in time.

        A device command sent while its client is still rebuilding the device fails, so a build waits for this.
        """
        clients = sorted(self._sent_devices_to)
        built = await asyncio.gather(*(self.wait_for_build(client_id, timeout) for client_id in clients))
        return [client_id for client_id, done in zip(clients, built) if not done]

    def for_client(self, client_id: str) -> DeviceAssignment | None:
        """None before any build has succeeded: the client waits."""
        if self._devices is None or self._assignment_id is None:
            return None
        return DeviceAssignment(
            assignment_id=self._assignment_id, devices=[d for d in self._devices if d.setup.client == client_id],
        )

    async def publish(self, devices: list[AssignedDevice]) -> None:
        """Take a new snapshot and send every connected client its part of it."""
        self._devices = list(devices)
        self._assignment_id = uuid.uuid4().hex
        self._reported = {}
        self._sent_devices_to = set()
        for client_id in self._connections.connected_client_ids():
            await self.send_to(client_id)

    async def send_to(self, client_id: str) -> None:
        assignment = self.for_client(client_id)
        if assignment is None:
            return
        if not assignment.devices:
            logger.warning("Client %s has no devices assigned to it in devices/*.json.", client_id)
        else:
            # Added before the send, so a report that comes back during it still counts.
            self._sent_devices_to.add(client_id)
        sent = await self._connections.send_to_client(client_id, MessageEnvelope.wrap_assignment(assignment).model_dump_json())
        if not sent:
            self._sent_devices_to.discard(client_id)

