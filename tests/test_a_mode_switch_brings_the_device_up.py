"""A switch between sim and live brings the device up in its new mode, so the next command never reaches a device nobody brought up."""

from unittest.mock import AsyncMock, patch

import pytest

from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode
from tests.device_modes import InMemoryDeviceModes
from tests.memory_activity import memory_runtime
from tests.mock import UniversalMockDevice
from tests.runtime.test_an_offline_live_device_refuses_a_run_at_its_start import _Instrument, _runtime
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build
from tests.recording_handler import RecordingHandler

pytestmark = pytest.mark.out_of_run


class _ModeRecordingInstrument(_Instrument):
    def __init__(self, name: str, *, answers: bool) -> None:
        super().__init__(name, powered=True)
        self.answers = answers
        self.brought_up_in: list[WorkflowRunMode] = []

    async def initialize(self) -> None:
        assert self.device is not None
        self.brought_up_in.append(await self.device.effective_mode())
        if not self.answers:
            raise ConnectionError(f"{self.name} does not answer")
        await super().initialize()


def _live_device(name: str, *, answers: bool) -> tuple[UniversalMockDevice, _ModeRecordingInstrument]:
    driver = _ModeRecordingInstrument(name, answers=answers)
    device = UniversalMockDevice(name, driver=driver)
    driver.device = device
    device.use_device_modes(InMemoryDeviceModes({name: DeviceMode.LIVE}))
    return device, driver


async def test_a_switch_initializes_the_device_in_its_new_mode() -> None:
    shaker, driver = _live_device("switch_init_a", answers=True)
    runtime, _workflow = await _runtime("switch_init", [shaker], ["pad1"])
    try:
        switch = await runtime.switch_device_mode("switch_init_a", DeviceMode.SIM, confirm=True)

        assert switch.switched
        assert switch.bring_up_error is None
        assert driver.brought_up_in == [WorkflowRunMode.SIM]
    finally:
        await runtime.shutdown()


async def test_a_switch_that_cannot_bring_the_device_up_still_switches_and_says_why() -> None:
    shaker, _driver = _live_device("switch_off_a", answers=False)
    runtime, _workflow = await _runtime("switch_off", [shaker], ["pad1"])
    try:
        switch = await runtime.switch_device_mode("switch_off_a", DeviceMode.SIM, confirm=True)

        assert switch.switched
        assert switch.bring_up_error is not None and "does not answer" in switch.bring_up_error
        assert await runtime.device_modes.mode_of("switch_off_a") is DeviceMode.SIM
    finally:
        await runtime.shutdown()


async def test_a_switch_reseeds_a_liquid_handlers_deck_from_the_ledger() -> None:
    recorder = RecordingHandler()
    build, lh = await _build(recorder, wf_name="deck_switch")
    lh.use_device_modes(InMemoryDeviceModes({lh.name: DeviceMode.LIVE}))
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    try:
        await runtime.labware.register("reservoir", location=RESERVOIR_SITE, confirm=True)
        configures = sum(1 for call in recorder.calls if call.method == "configure_deck")
        reconciles = [call for call in recorder.calls if call.method == "reconcile_deck_occupancy"]

        await runtime.switch_device_mode(lh.name, DeviceMode.SIM, confirm=True)

        after = [call for call in recorder.calls if call.method == "reconcile_deck_occupancy"]
        assert sum(1 for call in recorder.calls if call.method == "configure_deck") == configures + 1
        assert len(after) == len(reconciles) + 1
        assert "reservoir" in str(after[-1].args)
    finally:
        await runtime.shutdown()


async def test_a_switch_whose_stale_flag_cannot_be_saved_keeps_the_old_mode() -> None:
    """A switch marks what stands on the device STALE. A switch saved without that flag would trust state nobody watched."""
    recorder = RecordingHandler()
    build, lh = await _build(recorder, wf_name="stale_first")
    lh.use_device_modes(InMemoryDeviceModes({lh.name: DeviceMode.LIVE}))
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus
    )
    await runtime.start()
    try:
        with patch.object(
            build.system.mounted_tips, "note_observation_gap", AsyncMock(side_effect=OSError("disk full")),
        ), pytest.raises(OSError, match="disk full"):
            await runtime.switch_device_mode(lh.name, DeviceMode.SIM, confirm=True)

        assert await runtime.device_modes.mode_of(lh.name) is DeviceMode.LIVE
    finally:
        await runtime.shutdown()
