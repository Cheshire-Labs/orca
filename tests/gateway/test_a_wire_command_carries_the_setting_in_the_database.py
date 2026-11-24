"""A wire command carries the device's sim or live setting as the database holds it now."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from orca.devices.shaker import Shaker
from orca.gateway.controller.controller import DeviceController
from orca.gateway.mode_resolution import ResourceLookup, system_mode_resolver
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.resource_models.resources import IResource
from orca.runtime.db import create_sqlite_engine
from orca.driver_management.device_factory_context import use_device_factory
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode, mode_scope
from orca.runtime.sqlite_device_mode_store import SqliteDeviceModeStore


@dataclass
class _StampsRecorded:
    modes: list[WorkflowRunMode] = field(default_factory=list)

    async def execute_command(self, *, effective_mode: WorkflowRunMode, **_: JsonValue) -> JsonValue:
        self.modes.append(effective_mode)
        return None


@dataclass
class _OneDevice:
    device: IResource

    def has_resource(self, name: str) -> bool:
        return name == self.device.name

    def get_resource(self, name: str) -> IResource:
        return self.device


@dataclass
class _Runtime:
    system: ResourceLookup


@pytest.mark.asyncio
async def test_a_setting_another_writer_changed_is_what_the_next_command_carries(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    controller = _StampsRecorded()
    runtime: list[_Runtime] = []
    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, controller),
        mode_resolver=system_mode_resolver(lambda: runtime[0], base_mode=lambda: WorkflowRunMode.LIVE),
    )
    with use_device_factory(factory):
        shaker = Shaker("shaker_1")
    shaker.use_device_modes(SqliteDeviceModeStore(create_sqlite_engine(path)))
    runtime.append(_Runtime(_OneDevice(shaker)))
    another_writer = SqliteDeviceModeStore(create_sqlite_engine(path))

    with mode_scope(WorkflowRunMode.LIVE):
        await shaker.driver.initialize()
        await another_writer.set("shaker_1", DeviceMode.LIVE)
        await shaker.driver.initialize()

    assert controller.modes == [WorkflowRunMode.SIM, WorkflowRunMode.LIVE]
