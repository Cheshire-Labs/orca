"""The typed one-command device routes: served by every server, and bounded past what the driver model checks."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing_extensions import Self

import pytest
from cheshire_drivers.command_table import COMMANDS, DeviceCommand
from cheshire_drivers.liquid_handler_models import LabwareStateResponse
from cheshire_drivers.command_responses import EmptyCommandResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, JsonValue

from orca.daemon.app import create_app
from orca.daemon.daemon_state import DaemonState
from orca.daemon.operation_catalog import DaemonOperationCatalog
from orca.gateway.device_command_runner import NamedCommandResult
from orca.gateway.registry import DeviceSnapshot
from orca.operations.typed_device_commands import TYPED_COMMAND_OPERATIONS, TYPED_ROUTES, TypedRoute, _operation

_LABWARE_COMMANDS = {"drop_tips", "drop_tips96", "aspirate96", "dispense96"}


class _Runner:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, JsonValue]]] = []

    async def device(self, device_id: str) -> DeviceSnapshot:
        return DeviceSnapshot(
            type="device", name=device_id, interfaces=["ITransporter", "ILiquidHandler"], capabilities=[],
            provides_state=False, methods={}, site="s", lab="l", workcell=None, status="ready",
            last_seen=datetime.now(timezone.utc),
        )

    async def run(
        self, device_id: str, command: DeviceCommand, params: BaseModel, *, reason: str | None = None,
    ) -> BaseModel:
        self.sent.append((command.name, params.model_dump(mode="json", exclude_unset=True)))
        return LabwareStateResponse(success=True) if command.name in _LABWARE_COMMANDS else EmptyCommandResponse()

    async def run_named(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
    ) -> NamedCommandResult:
        raise AssertionError("a typed command runs by its table entry, never by name")

    @asynccontextmanager
    async def hold(self, device_id: str) -> AsyncIterator[Self]:
        yield self


def test_the_daemon_serves_every_typed_command() -> None:
    served = {entry.op_class for entry in DaemonOperationCatalog().entries()}
    mounted = {getattr(route, "path", "") for route in create_app().routes}

    assert set(TYPED_COMMAND_OPERATIONS) <= served
    assert "/api/devices/{device_id}/shake" in mounted


# Route, a body inside the bounds, and the one field that crosses a bound.
_CASES: list[tuple[str, dict[str, JsonValue], str, JsonValue]] = [
    ("devices/d1/shake", {"speed": 2000, "duration": 10}, "speed", 2001),
    ("devices/d1/shake", {"speed": 500, "duration": 10}, "speed", 0),
    ("devices/d1/shake", {"speed": 500, "duration": 10}, "duration", 0),
    ("devices/d1/centrifuge", {"g": 5000, "duration": 10}, "g", 5001),
    ("devices/d1/centrifuge", {"g": 500, "duration": 10}, "duration", 0),
    ("devices/d1/seal", {"temperature": 200, "duration": 10}, "temperature", 201),
    ("devices/d1/seal", {"temperature": 150, "duration": 10}, "duration", 0),
    ("devices/d1/set-temperature", {"temperature": -20}, "temperature", -21),
    ("devices/d1/set-temperature", {"temperature": 200}, "temperature", 201),
    ("transporters/d1/set-speed", {"speed": 1.0}, "speed", 1.5),
    ("transporters/d1/set-speed", {"speed": 0.0}, "speed", -0.1),
    ("liquid-handlers/d1/drop-tips", {"to_waste": True}, "drops", []),
    ("liquid-handlers/d1/drop-tips96", {"tip_rack": "r" * 100}, "tip_rack", "r" * 101),
    ("liquid-handlers/d1/aspirate96", {"labware": "p", "volume": 10}, "volume", 0),
    ("liquid-handlers/d1/aspirate96", {"labware": "p", "volume": 10}, "flow_rate", 0),
    ("liquid-handlers/d1/aspirate96", {"labware": "p", "volume": 10}, "liquid_height", -1),
    ("liquid-handlers/d1/dispense96", {"labware": "p", "volume": 10}, "volume", 0),
    ("liquid-handlers/d1/dispense96", {"labware": "p", "volume": 10}, "flow_rate", 0),
    ("liquid-handlers/d1/dispense96", {"labware": "p", "volume": 10}, "liquid_height", -1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "body", "field", "past"), _CASES)
async def test_a_value_past_the_bound_is_refused_and_one_inside_is_sent(
    route: str, body: dict[str, JsonValue], field: str, past: JsonValue,
) -> None:
    app = create_app()
    daemon: DaemonState = app.state.daemon
    runner = _Runner()
    daemon.device_commands = runner

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon") as client:
        inside = await client.post(f"/api/{route}", json=body)
        refused = await client.post(f"/api/{route}", json={**body, field: past})

    assert inside.status_code == 200, inside.text
    assert refused.status_code == 422, refused.text
    assert len(runner.sent) == 1


def test_no_typed_route_serves_a_driver_property() -> None:
    """A property is a reading the device registry holds, so it is never a command route."""
    properties = [
        route.path for route in TYPED_ROUTES
        if COMMANDS.commands_of(route.interface)[route.command].is_property
    ]
    assert properties == []
    with pytest.raises(ValueError, match="device registry"):
        _operation(TypedRoute("/devices/{device_id}/is-initialized", "IShaker", "is_initialized"))
