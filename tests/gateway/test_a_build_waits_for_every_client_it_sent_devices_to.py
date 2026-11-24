"""After a build, the server can wait for every connected client it sent devices to, and learns which ones are late."""

import asyncio

from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.gateway_protocol import AssignedDevice

from orca.gateway.websocket.assignments import DeviceAssignments


class _Connected:
    def __init__(self, *client_ids: str) -> None:
        self._ids = list(client_ids)
        self.sent: list[str] = []

    def connected_client_ids(self) -> list[str]:
        return list(self._ids)

    async def send_to_client(self, client_id: str, message: str) -> bool:
        self.sent.append(client_id)
        return True


def _device(name: str, client: str) -> AssignedDevice:
    return AssignedDevice(name=name, kind="shaker", setup=DeviceSetup.model_validate({"client": client}))


def _latest_id(table: DeviceAssignments, client_id: str) -> str:
    assignment = table.for_client(client_id)
    assert assignment is not None
    return assignment.assignment_id


async def test_the_wait_ends_once_every_connected_client_with_devices_has_built() -> None:
    table = DeviceAssignments(_Connected("lab_pc", "bench_pc", "idle_pc"))
    await table.publish([_device("shaker_1", "lab_pc"), _device("shaker_2", "bench_pc"), _device("arm", "offline_pc")])

    waiting = asyncio.ensure_future(table.wait_for_every_sent_build(timeout=5.0))
    table.record_build("lab_pc", _latest_id(table, "lab_pc"), applied=True)
    await asyncio.sleep(0)
    assert not waiting.done(), "bench_pc has not built yet"
    table.record_build("bench_pc", _latest_id(table, "bench_pc"), applied=True)

    assert await waiting == []


async def test_a_client_that_does_not_build_in_time_is_named() -> None:
    table = DeviceAssignments(_Connected("lab_pc", "bench_pc"))
    await table.publish([_device("shaker_1", "lab_pc"), _device("shaker_2", "bench_pc")])
    table.record_build("lab_pc", _latest_id(table, "lab_pc"), applied=True)

    assert await table.wait_for_every_sent_build(timeout=0.05) == ["bench_pc"]


class _DroppedDuringTheSend(_Connected):
    async def send_to_client(self, client_id: str, message: str) -> bool:
        self.sent.append(client_id)
        return client_id != "bench_pc"


async def test_a_client_the_send_did_not_reach_is_not_waited_for() -> None:
    table = DeviceAssignments(_DroppedDuringTheSend("lab_pc", "bench_pc"))
    await table.publish([_device("shaker_1", "lab_pc"), _device("shaker_2", "bench_pc")])
    table.record_build("lab_pc", _latest_id(table, "lab_pc"), applied=True)

    assert await table.wait_for_every_sent_build(timeout=5.0) == []
