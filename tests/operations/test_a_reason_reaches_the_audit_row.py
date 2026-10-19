"""An operator's optional reason reaches the audited facade call.

These requests had no reason field, so an audited verb wrote its row with no
reason at all. Leaving the reason out is still accepted.
"""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError

from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.operations import device, execution, labware, thread
from orca.operations.device_models import (
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
from orca.runtime.danger import clear_audit_trail, list_audit_entries
from orca.runtime.facades.devices import DeviceFacade
from orca.runtime.facades.labware import LabwareFacade
from orca.runtime.facades.submissions import SubmissionFacade
from orca.runtime.facades.threads import ThreadFacade
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system

Run = Callable[[MagicMock], Awaitable[BaseModel]]

FACADES: dict[str, type] = {
    "devices": DeviceFacade,
    "labware": LabwareFacade,
    "submissions": SubmissionFacade,
    "threads": ThreadFacade,
}

CASES: list[tuple[str, str, Run]] = [
    ("devices", "initialize",
     lambda rt: device.InitializeDeviceOperation(rt).run(InitializeDeviceRequest(device_name="d", reason="r"))),
    ("devices", "disconnect",
     lambda rt: device.DisconnectDeviceOperation(rt).run(DisconnectDeviceRequest(device_name="d", reason="r"))),
    ("devices", "reconcile_deck",
     lambda rt: device.ReconcileDeckOperation(rt).run(ReconcileDeckRequest(device_name="d", reason="r"))),
    ("devices", "release_external_control",
     lambda rt: device.ReleaseDeviceControlOperation(rt).run(ReleaseDeviceControlRequest(device_name="d", reason="r"))),
    ("devices", "clear_fault",
     lambda rt: device.ClearDeviceFaultOperation(rt).run(ClearDeviceFaultRequest(device_name="d", reason="r"))),
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
    rt = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await rt.start()
    try:
        yield rt
    finally:
        await rt.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_the_reason_lands_on_the_row(runtime: SystemRuntime) -> None:
    clear_audit_trail()

    await device.ClearDeviceFaultOperation(runtime).run(
        ClearDeviceFaultRequest(device_name="shaker1", reason="looked at it"),
    )

    [row] = [r for r in list_audit_entries() if r.action_name == "device.clear_fault"]
    assert row.reason == "looked at it"


@pytest.mark.asyncio
async def test_leaving_the_reason_out_is_still_accepted(runtime: SystemRuntime) -> None:
    clear_audit_trail()

    await device.ClearDeviceFaultOperation(runtime).run(
        ClearDeviceFaultRequest(device_name="shaker1"),
    )

    [row] = [r for r in list_audit_entries() if r.action_name == "device.clear_fault"]
    assert row.reason is None
