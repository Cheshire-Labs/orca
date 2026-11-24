"""A device set to sim may not have a position captured unless it has no real positions to protect."""

from unittest.mock import MagicMock

import pytest
from cheshire_drivers.device_setup import DeviceSetup, RealDriverSetup

from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.facades.device_modes import DeviceModeFacade
from tests.device_modes import InMemoryDeviceModes

_REAL_ARM = DeviceSetup(client="lab_pc", real=RealDriverSetup(type="human"))


class _Setups:
    def __init__(self, setup: DeviceSetup) -> None:
        self._setup = setup

    @property
    def default_client(self) -> str:
        return "lab_pc"

    def names(self) -> list[str]:
        return ["arm"]

    def read(self, name: str) -> DeviceSetup:
        return self._setup


def _facade(setting: DeviceMode, setup: DeviceSetup) -> DeviceModeFacade:
    return DeviceModeFacade(InMemoryDeviceModes({"arm": setting}), _Setups(setup), MagicMock())


@pytest.mark.parametrize(("setting", "setup", "allowed"), [
    (DeviceMode.LIVE, _REAL_ARM, True),
    (DeviceMode.SIM, _REAL_ARM, False),
    (DeviceMode.SIM, _REAL_ARM.model_copy(update={"always_sim": True}), True),
    (DeviceMode.SIM, DeviceSetup(), True),
])
@pytest.mark.asyncio
async def test_capture_is_allowed_live_or_on_a_device_with_no_real_positions(
    setting: DeviceMode, setup: DeviceSetup, allowed: bool,
) -> None:
    assert await _facade(setting, setup).may_capture("arm", under=WorkflowRunMode.LIVE) is allowed


@pytest.mark.parametrize(("setup", "allowed"), [
    (_REAL_ARM, False),
    (_REAL_ARM.model_copy(update={"always_sim": True}), True),
])
@pytest.mark.asyncio
async def test_a_live_device_reads_in_sim_while_a_sim_run_is_active_so_capture_is_refused(
    setup: DeviceSetup, allowed: bool,
) -> None:
    """A sim run, running or paused, sends an operator's read to the simulator; its position is not the arm's."""
    assert await _facade(DeviceMode.LIVE, setup).may_capture("arm", under=WorkflowRunMode.SIM) is allowed
