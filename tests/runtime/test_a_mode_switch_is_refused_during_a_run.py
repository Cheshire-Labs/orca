"""A device cannot switch between sim and live while a run is going or starting, so a run never changes world part-way."""

import asyncio

import pytest

from orca.gateway.controller.exceptions import DeviceLockedError
from orca.modes.device_modes import DeviceMode
from orca.runtime.facades.device_modes import ModeSwitchDuringRunError
from orca.modes.run_modes import WorkflowRunMode
from tests.mock import UniversalMockDevice
from tests.device_modes import InMemoryDeviceModes
from tests.runtime.test_an_offline_live_device_refuses_a_run_at_its_start import _Instrument, _device, _runtime, _until


class _SlowToComeUp(_Instrument):
    """Its initialize waits until the test lets it go, so the test can act mid bring-up."""

    def __init__(self, name: str) -> None:
        super().__init__(name, powered=True)
        self.release = asyncio.Event()
        self.initializing = asyncio.Event()
        self.brought_up_in: list[WorkflowRunMode] = []

    async def initialize(self) -> None:
        assert self.device is not None
        self.brought_up_in.append(await self.device.effective_mode())
        self.initializing.set()
        await self.release.wait()
        await super().initialize()


def _slow_device(name: str, setting: DeviceMode) -> tuple[UniversalMockDevice, _SlowToComeUp]:
    driver = _SlowToComeUp(name)
    device = UniversalMockDevice(name, driver=driver)
    driver.device = device
    device.use_device_modes(InMemoryDeviceModes({name: setting}))
    return device, driver


async def test_a_switch_during_a_run_is_refused_and_names_the_run() -> None:
    shaker, driver = _device("mid_run_a", powered=True, setting=DeviceMode.LIVE)
    driver.hold = asyncio.Event()
    runtime, workflow = await _runtime("mid_run", [shaker], ["pad1"])
    try:
        record = await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)
        await _until(lambda: driver.shakes == 1)

        with pytest.raises(ModeSwitchDuringRunError) as refused:
            await runtime.switch_device_mode("mid_run_a", DeviceMode.SIM, confirm=True)

        assert record.id in str(refused.value)
        assert await runtime.device_modes.mode_of("mid_run_a") is DeviceMode.LIVE
    finally:
        driver.hold.set()
        await runtime.shutdown()


async def test_during_a_run_the_same_mode_and_an_unknown_device_answer_their_own_way() -> None:
    shaker, driver = _device("own_way_a", powered=True, setting=DeviceMode.LIVE)
    driver.hold = asyncio.Event()
    runtime, workflow = await _runtime("own_way", [shaker], ["pad1"])
    try:
        await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)
        await _until(lambda: driver.shakes == 1)

        unchanged = await runtime.switch_device_mode("own_way_a", DeviceMode.LIVE, confirm=True)
        with pytest.raises(KeyError):
            await runtime.switch_device_mode("no_such_device", DeviceMode.SIM, confirm=True)

        assert (unchanged.switched, unchanged.warning) == (False, None)
    finally:
        driver.hold.set()
        await runtime.shutdown()



async def _let_everything_else_run() -> None:
    """Yield until every other ready task has run as far as it can without the held device."""
    for _ in range(100):
        await asyncio.sleep(0)


async def test_a_switch_sent_while_a_run_is_starting_waits_for_it_then_is_refused() -> None:
    shaker, driver = _slow_device("starting_a", DeviceMode.LIVE)
    runtime, workflow = await _runtime("starting", [shaker], ["pad1"])
    try:
        submit = asyncio.create_task(runtime.submit_workflow(workflow, mode=WorkflowRunMode.SIM))
        await driver.initializing.wait()
        switch = asyncio.create_task(runtime.switch_device_mode("starting_a", DeviceMode.SIM, confirm=True))
        await _let_everything_else_run()
        assert not switch.done(), "the switch must wait for the run's start, not slip in before the run exists"

        driver.release.set()
        await submit
        with pytest.raises(ModeSwitchDuringRunError):
            await switch
        assert await runtime.device_modes.mode_of("starting_a") is DeviceMode.LIVE
    finally:
        driver.release.set()
        await runtime.shutdown()


@pytest.mark.out_of_run
async def test_a_run_sent_while_a_switch_is_bringing_the_device_up_starts_in_the_new_mode() -> None:
    shaker, driver = _slow_device("switching_a", DeviceMode.LIVE)
    runtime, workflow = await _runtime("switching", [shaker], ["pad1"])
    try:
        switch = asyncio.create_task(runtime.switch_device_mode("switching_a", DeviceMode.SIM, confirm=True))
        await driver.initializing.wait()
        submit = asyncio.create_task(runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE))
        await _let_everything_else_run()
        assert not submit.done(), "the run must wait for the switch's bring-up"

        driver.release.set()
        assert (await switch).switched
        await submit

        assert driver.brought_up_in == [WorkflowRunMode.SIM]
    finally:
        driver.release.set()
        await runtime.shutdown()


async def test_a_switch_is_refused_while_a_command_holds_the_device() -> None:
    shaker, _driver = _device("held_a", powered=True, setting=DeviceMode.LIVE)
    runtime, _workflow = await _runtime("held", [shaker], ["pad1"])
    try:
        async with shaker.lock.held_for("operator shake"):
            with pytest.raises(DeviceLockedError, match="operator shake"):
                await runtime.switch_device_mode("held_a", DeviceMode.SIM, confirm=True)

        assert await runtime.device_modes.mode_of("held_a") is DeviceMode.LIVE
    finally:
        await runtime.shutdown()


async def test_without_confirm_nothing_switches_and_the_warning_comes_back() -> None:
    shaker, _driver = _device("unconfirmed_a", powered=True, setting=DeviceMode.LIVE)
    runtime, _workflow = await _runtime("unconfirmed", [shaker], ["pad1"])
    try:
        asked = await runtime.switch_device_mode("unconfirmed_a", DeviceMode.SIM, confirm=False)

        assert not asked.switched
        assert asked.warning is not None and "carries over" in asked.warning
        assert await runtime.device_modes.mode_of("unconfirmed_a") is DeviceMode.LIVE
    finally:
        await runtime.shutdown()
