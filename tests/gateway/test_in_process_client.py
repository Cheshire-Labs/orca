"""A check run's orca-client runs inside Orca, with the same simulators and executor orca-client uses."""

import asyncio
from pathlib import Path
from typing import Callable

import pytest
from cheshire_drivers.device_setup import DeviceSetup, RealDriverSetup, SimDriverSetup
from cheshire_drivers.gateway_protocol import AssignedDevice
from cheshire_drivers.command_executor import DeviceRegistry
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import SimShakerDriver

from orca.gateway.controller.exceptions import CommandExecutionError, CommandTimeoutError, DeviceOfflineError
from orca.gateway.in_process_client import InProcessClient
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.startup import CheckRunDevices, PackageSetupStoreFactory, build_check_run_devices
from orca.runtime.server_events import ServerEvents

pytestmark = pytest.mark.asyncio


def _devices(tmp_path: Path) -> CheckRunDevices:
    return build_check_run_devices(PackageSetupStoreFactory(tmp_path, default_client="local"), lambda: None, ServerEvents())


async def test_a_check_run_command_runs_on_the_devices_simulator(tmp_path: Path) -> None:
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])
    try:
        simulator = devices.client.simulator("shaker_1")
        assert isinstance(simulator, SimShakerDriver)

        await devices.connections.gateway.controller.execute_command(
            "shaker_1", "shake", {"speed": 500, "duration": 1.0},
            effective_mode=WorkflowRunMode.CHECK_RUN,
        )

        assert await devices.connections.connection_source.is_connected("shaker_1")
    finally:
        await devices.client.aclose()


async def test_the_check_runs_client_has_a_driver_only_for_a_check_run_command(tmp_path: Path) -> None:
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])
    try:
        for mode, refusal in ((WorkflowRunMode.SIM, "no driver for SIM commands"), (WorkflowRunMode.LIVE, "not found")):
            with pytest.raises(CommandExecutionError, match=refusal):
                await devices.connections.gateway.controller.execute_command(
                    "shaker_1", "shake", {"speed": 500, "duration": 1.0}, effective_mode=mode,
                )
        await devices.connections.gateway.controller.execute_command(
            "shaker_1", "shake", {"speed": 500, "duration": 1.0}, effective_mode=WorkflowRunMode.CHECK_RUN,
        )
    finally:
        await devices.client.aclose()


async def test_a_vendor_simulator_this_server_cannot_run_refuses_the_device(tmp_path: Path) -> None:
    """Never a plain simulator in its place: the device has no simulator here, so a command to it is refused."""
    vendor_sim = DeviceSetup(
        real=RealDriverSetup(type="plr", backend="OpentronsFlex"),
        sim=SimDriverSetup(use_vendor_sim=True, simulator_config="flex.json"),
    )
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="flex_1", kind="liquid_handler", deck_modeling=True, setup=vendor_sim)])
    try:
        assert devices.client.simulator("flex_1") is None
        assert devices.connections.gateway.tracker.build_error("flex_1") is not None
        with pytest.raises(DeviceOfflineError):
            await devices.connections.gateway.controller.execute_command(
                "flex_1", "get_deck_state", {}, effective_mode=WorkflowRunMode.CHECK_RUN,
            )
    finally:
        await devices.client.aclose()


async def test_a_command_the_client_cannot_run_is_answered_at_once_not_left_to_time_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])

    def no_lock(self: DeviceRegistry, device_name: str) -> asyncio.Lock | None:
        raise RuntimeError("the lock table is gone")

    monkeypatch.setattr(DeviceRegistry, "lock_for", no_lock)
    try:
        with pytest.raises(CommandExecutionError, match="lock table"):
            await asyncio.wait_for(devices.connections.gateway.controller.execute_command(
                "shaker_1", "shake", {"speed": 500, "duration": 1.0}, timeout_seconds=600.0,
                effective_mode=WorkflowRunMode.CHECK_RUN,
            ), timeout=5.0)
    finally:
        await devices.client.aclose()


async def test_the_client_reports_what_it_sees_on_each_device(tmp_path: Path) -> None:
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])
    try:
        snapshot = await devices.connections.gateway.tracker.get_device("shaker_1")
        assert snapshot is not None
        assert (snapshot.link_mode, snapshot.status) == ("SIM", "ready")
    finally:
        await devices.client.aclose()


async def test_a_command_that_times_out_is_cancelled_on_the_simulator(tmp_path: Path) -> None:
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])
    simulator = devices.client.simulator("shaker_1")
    started, never = asyncio.Event(), asyncio.Event()
    cancelled: list[bool] = []

    async def stuck_shake(request: ShakeRequest) -> None:
        started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    setattr(simulator, "shake", stuck_shake)
    try:
        with pytest.raises(CommandTimeoutError):
            await devices.connections.gateway.controller.execute_command(
                "shaker_1", "shake", {"speed": 500, "duration": 1.0}, timeout_seconds=0.2,
                effective_mode=WorkflowRunMode.CHECK_RUN,
            )
        await asyncio.wait_for(_until_true(lambda: bool(cancelled)), timeout=5.0)
    finally:
        await devices.client.aclose()


async def test_ending_a_check_run_releases_every_simulator(tmp_path: Path) -> None:
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])

    await devices.client.aclose()

    assert devices.client.simulator("shaker_1") is None


async def _until_true(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0)


async def test_a_device_left_out_of_the_next_assignment_has_its_simulator_released(tmp_path: Path) -> None:
    devices = _devices(tmp_path)
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker"), AssignedDevice(name="shaker_2", kind="shaker")])
    dropped = devices.client.simulator("shaker_2")
    assert isinstance(dropped, SimShakerDriver)
    released: list[bool] = []
    original = dropped._shutdown

    async def recording_shutdown() -> None:
        released.append(True)
        await original()

    setattr(dropped, "_shutdown", recording_shutdown)
    try:
        await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])

        assert released == [True]
        assert devices.client.simulator("shaker_2") is None
        assert devices.client.simulator("shaker_1") is not None
    finally:
        await devices.client.aclose()


async def test_a_second_assignment_runs_exactly_its_devices_and_keeps_one_heartbeat(tmp_path: Path) -> None:
    devices = _devices(tmp_path)
    shaker_1 = AssignedDevice(name="shaker_1", kind="shaker")
    await devices.client.run([shaker_1])
    first = devices.client.simulator("shaker_1")
    await devices.client.run([shaker_1, AssignedDevice(name="shaker_2", kind="shaker")])
    beats = [t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__qualname__", None) == "InProcessClient._beat"]
    both = [devices.client.simulator(name) for name in ("shaker_1", "shaker_2")]
    await devices.client.aclose()

    assert both[0] is first, "a device the second assignment still lists keeps its simulator"
    assert all(isinstance(driver, SimShakerDriver) for driver in both)
    assert len(beats) == 1
    assert all(t.done() for t in beats)


async def test_a_check_run_given_a_simulator_runs_the_device_on_it(tmp_path: Path) -> None:
    given = SimShakerDriver("shaker_1")
    devices = build_check_run_devices(
        PackageSetupStoreFactory(tmp_path, default_client="local"), lambda: None, ServerEvents(),
        lambda tracker: InProcessClient(tracker, lambda _request: given),
    )
    await devices.client.run([AssignedDevice(name="shaker_1", kind="shaker")])
    try:
        assert devices.client.simulator("shaker_1") is given
    finally:
        await devices.client.aclose()
