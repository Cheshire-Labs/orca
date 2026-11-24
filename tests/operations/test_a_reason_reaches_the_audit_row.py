"""An operator's optional reason reaches the audited facade call.

These requests had no reason field, so an audited verb wrote its row with no
reason at all. Leaving the reason out is still accepted.
"""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing_extensions import Self
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pydantic import BaseModel, JsonValue, ValidationError

from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.command_table import DeviceCommand

from orca.gateway.device_command_runner import NamedCommandResult
from orca.gateway.registry import DeviceSnapshot
from orca.operations import device, execution, labware, thread
from orca.operations.device_models import (
    ConnectDeviceRequest,
    ClearDeviceFaultRequest,
    DisconnectDeviceRequest,
    InitializeDeviceRequest,
    ReconcileDeckRequest,
    ReleaseDeviceControlRequest,
)
from orca.operations.execution_models import CloseExecutionRequest
from orca.operations.labware_models import (
    ClearLabwareCarryOverrideRequest,
    EditLabwareBarcodeRequest,
    RegisterLabwareRequest,
    SetLabwareCarryOverrideRequest,
)
from orca.operations.thread_models import SpawnThreadRequest
from orca.runtime.facades.devices import DeviceFacade
from orca.runtime.facades.labware import LabwareFacade
from orca.runtime.facades.submissions import SubmissionFacade
from orca.runtime.facades.threads import ThreadFacade
from orca.runtime.system_runtime import SystemRuntime

from tests.audit_capture import AuditCapture
from tests.test_system_runtime import _build_simple_system
from tests.memory_activity import memory_runtime

Run = Callable[[MagicMock], Awaitable[BaseModel]]


class _Runner:
    """Runs every command it is sent and holds every device it is asked to."""

    def __init__(self) -> None:
        self.named: list[tuple[str, str, str | None]] = []

    async def device(self, device_id: str) -> DeviceSnapshot:
        raise AssertionError("these verbs never read the device")

    async def run(
        self, device_id: str, command: DeviceCommand, params: BaseModel, *, reason: str | None = None,
    ) -> BaseModel:
        raise AssertionError("these verbs run their command by name")

    async def run_named(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
    ) -> NamedCommandResult:
        self.named.append((device_id, command, reason))
        return NamedCommandResult(duration_ms=0.0)

    @asynccontextmanager
    async def hold(self, device_id: str) -> AsyncIterator[Self]:
        yield self

FACADES: dict[str, type] = {
    "devices": DeviceFacade,
    "labware": LabwareFacade,
    "submissions": SubmissionFacade,
    "threads": ThreadFacade,
}

CASES: list[tuple[str, str, Run]] = [
    ("devices", "reconcile_deck",
     lambda rt: device.ReconcileDeckOperation(runtime=rt, devices=_Runner()).run(
         ReconcileDeckRequest(device_id="d", reason="r"),
     )),
    ("devices", "release_external_control",
     lambda rt: device.ReleaseDeviceControlOperation(rt).run(ReleaseDeviceControlRequest(device_id="d", reason="r"))),
    ("devices", "clear_fault",
     lambda rt: device.ClearDeviceFaultOperation(rt).run(ClearDeviceFaultRequest(device_id="d", reason="r"))),
    ("submissions", "close_execution",
     lambda rt: execution.CloseExecutionOperation(rt).run(CloseExecutionRequest(execution_id="e", reason="r"))),
    ("labware", "edit_barcode",
     lambda rt: labware.EditLabwareBarcodeOperation(rt).run(EditLabwareBarcodeRequest(labware_id="l", new_barcode="b", reason="r"))),
    ("labware", "set_carry_override",
     lambda rt: labware.SetLabwareCarryOverrideOperation(rt).run(SetLabwareCarryOverrideRequest(
         labware_id="l", set=MoveParameterPatch(speed=30.0), reason="r",
     ))),
    ("labware", "clear_carry_override",
     lambda rt: labware.ClearLabwareCarryOverrideOperation(rt).run(ClearLabwareCarryOverrideRequest(labware_id="l", reason="r"))),
    ("labware", "register",
     lambda rt: labware.RegisterLabwareOperation(rt).run(RegisterLabwareRequest(template_name="t", reason="r"))),
    ("threads", "spawn_thread",
     lambda rt: thread.SpawnThreadOperation(rt).run(SpawnThreadRequest(execution_id="e", template_name="t", reason="r"))),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facade,method,run", CASES, ids=[f"{c[0]}.{c[1]}" for c in CASES],
)
async def test_the_operation_hands_the_reason_to_the_audited_call(
    facade: str, method: str, run: Run,
) -> None:
    # The call is bound against the real method's signature, so a facade that
    # does not take what the Operation passes records nothing and fails below.
    inner = getattr(FACADES[facade], method).__wrapped__
    real = inspect.signature(inner)
    calls: list[tuple[inspect.BoundArguments, bool]] = []
    mock_type = AsyncMock if inspect.iscoroutinefunction(inner) else MagicMock
    call = mock_type(side_effect=lambda *args, confirm=False, **kwargs: (
        calls.append((real.bind(None, *args, **kwargs), confirm)) or MagicMock()
    ))
    runtime = MagicMock()
    setattr(getattr(runtime, facade), method, call)

    try:
        await run(runtime)
    except (ValidationError, TypeError, AttributeError):
        pass  # shaping a response from the mock can fail; the call is checked below

    [(bound, confirmed)] = calls
    assert bound.arguments["reason"] == "r"
    assert confirmed is True


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[SystemRuntime]:
    system, _ = await _build_simple_system()
    rt = await memory_runtime(system)
    await rt.start()
    try:
        yield rt
    finally:
        await rt.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_the_reason_lands_on_the_row(audit: AuditCapture, runtime: SystemRuntime) -> None:
    await device.ClearDeviceFaultOperation(runtime).run(
        ClearDeviceFaultRequest(device_id="shaker1", reason="looked at it"),
    )

    [row] = [r for r in audit.rows() if r.action_name == "device.clear_fault"]
    assert row.reason == "looked at it"


@pytest.mark.asyncio
async def test_leaving_the_reason_out_is_still_accepted(audit: AuditCapture, runtime: SystemRuntime) -> None:
    await device.ClearDeviceFaultOperation(runtime).run(
        ClearDeviceFaultRequest(device_id="shaker1"),
    )

    [row] = [r for r in audit.rows() if r.action_name == "device.clear_fault"]
    assert row.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("send,command", [
    (lambda rt, r: device.InitializeDeviceOperation(runtime=rt, devices=r).run(
        InitializeDeviceRequest(device_id="shaker1", reason="bring up"),
    ), "initialize"),
    (lambda rt, r: device.ConnectDeviceOperation(runtime=rt, devices=r).run(
        ConnectDeviceRequest(device_id="shaker1", reason="bring up"),
    ), "connect"),
    (lambda rt, r: device.DisconnectDeviceOperation(runtime=rt, devices=r).run(
        DisconnectDeviceRequest(device_id="shaker1", reason="bring up"),
    ), "disconnect"),
], ids=["initialize", "connect", "disconnect"])
async def test_a_lifecycle_verb_goes_through_the_runner_with_its_reason(
    runtime: SystemRuntime, send: Callable[[SystemRuntime, _Runner], Awaitable[BaseModel]], command: str,
) -> None:
    runner = _Runner()

    await send(runtime, runner)

    assert runner.named == [("shaker1", command, "bring up")]
