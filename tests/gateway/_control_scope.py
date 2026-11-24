"""Operator control for tests that pin dispatch, not the topology.

The runner opens ``orca.gateway.operator_commands.operator_control``, which refuses when the runtime is
not built: correct in production, and not what a unit test over the batch's own behaviour is pinning. ``stub_control_scope``
resolves every device against an empty runtime and keeps the gateway's
controller, so the gate under test is the one the test names.
"""

from orca.resource_models.command_effects import CommandStatement
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest
from pydantic import JsonValue

from orca.events.event_bus import EventBus
from orca.gateway.operator_commands import OperatorControl
from orca.gateway.device_gateway import DeviceGateway
from tests.no_devices import NullGatewayRegistry
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ISystemRuntime
from orca.sdk.build import Topology
from orca.sdk.package import build_system
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.setup_package import package_stores


@asynccontextmanager
async def empty_runtime() -> AsyncIterator[ISystemRuntime]:
    """A started runtime whose topology declares no device."""
    build = await build_system(
        "operator_control", Topology(locations={}, transporters=[]), package_stores(),
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus, gateway_registry=NullGatewayRegistry())
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.shutdown()


class StatesNothing:
    """An operator-command recorder's reading part, for a command that moves no labware and passes every check."""

    def statement_of(
        self, device_id: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> CommandStatement | None:
        return None

    def labware_named_by(
        self, device_id: str, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> list[str]:
        return []

    async def check_before_sending(
        self, command: str, params: Mapping[str, JsonValue], interfaces: frozenset[str],
    ) -> None:
        return None


class KeptCommands(StatesNothing):
    """An operator-command recorder that keeps each completed command in a list."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []

    async def save_completed(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, JsonValue],
        result: JsonValue,
        interfaces: frozenset[str],
    ) -> None:
        del params, result, interfaces
        self.saved.append((device_id, command))


def stub_control_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the runner's scope at a resolved, un-flagged device."""

    @asynccontextmanager
    async def _scope(
        gateway: DeviceGateway, runtime: ISystemRuntime | None, device_id: str,
    ) -> AsyncIterator[OperatorControl]:
        del runtime
        device = MagicMock()
        device.interfaces = []
        async with empty_runtime() as empty:
            yield OperatorControl(
                empty, device_id, device, None, WorkflowRunMode.LIVE, KeptCommands(), gateway.controller,
            )

    monkeypatch.setattr("orca.gateway.operator_commands.operator_control", _scope)
