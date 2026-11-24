"""Each device is set to sim or live in the activity database; one with no setting is sim."""

from pathlib import Path

import pytest

from orca.runtime.db import create_sqlite_engine
from orca.modes.device_modes import DeviceMode
from orca.runtime.sqlite_device_mode_store import SqliteDeviceModeStore


def _opened(path: Path) -> SqliteDeviceModeStore:
    return SqliteDeviceModeStore(create_sqlite_engine(path))


@pytest.mark.asyncio
async def test_a_device_with_no_setting_is_sim(tmp_path: Path) -> None:
    assert await _opened(tmp_path / "state.db").mode_of("shaker1") is DeviceMode.SIM


@pytest.mark.asyncio
async def test_a_setting_survives_a_restart(tmp_path: Path) -> None:
    await _opened(tmp_path / "state.db").set("shaker1", DeviceMode.LIVE)

    reopened = _opened(tmp_path / "state.db")

    assert await reopened.mode_of("shaker1") is DeviceMode.LIVE
    assert await reopened.mode_of("robot1") is DeviceMode.SIM


@pytest.mark.asyncio
async def test_setting_a_device_back_to_sim_replaces_its_row(tmp_path: Path) -> None:
    modes = _opened(tmp_path / "state.db")
    await modes.set("shaker1", DeviceMode.LIVE)
    await modes.set("shaker1", DeviceMode.SIM)

    assert await _opened(tmp_path / "state.db").mode_of("shaker1") is DeviceMode.SIM
