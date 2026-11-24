"""A refusal for something missing names the thing that is actually missing, not every thing the request named."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations.labware import (
    EditLabwareLocationOperation, ReleaseMoverHoldOperation, ResetLabwareLocationOperation,
)
from orca.operations.labware_models import (
    EditLabwareLocationRequest, ReleaseMoverHoldRequest, ResetLabwareLocationRequest,
)
from orca.operations.protocol import OperationError
from orca.operations.thread import SpawnThreadOperation
from orca.operations.thread_models import SpawnThreadRequest
from orca.runtime.errors import (
    ExecutionNotFoundError, LabwareNotFoundError, ThreadTemplateNotFoundError, UnknownLocationError,
)


def _labware_raising(method: str, error: Exception) -> MagicMock:
    runtime = MagicMock()
    setattr(runtime.labware, method, AsyncMock(side_effect=error))
    return runtime


@pytest.mark.parametrize("method", ["edit_location", "reset_location"])
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LabwareNotFoundError("lw1"), "labware_not_found"),
        (UnknownLocationError("nowhere"), "location_not_found"),
    ],
)
async def test_a_location_statement_names_the_labware_or_the_location(
    method: str, error: Exception, code: str,
) -> None:
    runtime = _labware_raising(method, error)
    with pytest.raises(OperationError) as refused:
        if method == "edit_location":
            await EditLabwareLocationOperation(runtime).run(
                EditLabwareLocationRequest(labware_id="lw1", location="nowhere", reason="moved"),
            )
        else:
            await ResetLabwareLocationOperation(runtime).run(
                ResetLabwareLocationRequest(labware_id="lw1", location="nowhere", reason="moved"),
            )
    assert (refused.value.surface_code(), refused.value.http_status) == (code, 404)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ExecutionNotFoundError("Execution 'e1' not found"), "execution_not_found"),
        (ThreadTemplateNotFoundError("No thread template named 'tips'"), "thread_template_not_found"),
        (LabwareNotFoundError("lw1"), "labware_not_found"),
    ],
)
async def test_a_spawn_names_the_execution_the_template_or_the_labware(error: Exception, code: str) -> None:
    runtime = MagicMock()
    runtime.threads.spawn_thread = AsyncMock(side_effect=error)
    with pytest.raises(OperationError) as refused:
        await SpawnThreadOperation(runtime).run(
            SpawnThreadRequest(execution_id="e1", template_name="tips", labware_id="lw1", reason="recover"),
        )
    assert (refused.value.surface_code(), refused.value.http_status) == (code, 404)


async def test_a_spawn_keeps_the_engine_text_for_an_execution_it_cannot_find() -> None:
    runtime = MagicMock()
    runtime.threads.spawn_thread = AsyncMock(side_effect=ExecutionNotFoundError("Execution 'e1' not found"))
    with pytest.raises(OperationError) as refused:
        await SpawnThreadOperation(runtime).run(SpawnThreadRequest(execution_id="e1", template_name="tips", reason="r"))
    assert refused.value.message == "Execution 'e1' not found"


async def test_a_spawn_does_not_call_an_untyped_key_error_a_missing_execution() -> None:
    runtime = MagicMock()
    runtime.threads.spawn_thread = AsyncMock(side_effect=KeyError("some other lookup"))
    with pytest.raises(KeyError):
        await SpawnThreadOperation(runtime).run(SpawnThreadRequest(execution_id="e1", template_name="tips", reason="r"))


@pytest.mark.parametrize(
    ("error", "code"),
    [(LabwareNotFoundError("lw1"), "labware_not_found"), (KeyError("No mover named 'arm'"), "mover_not_found")],
)
async def test_a_mover_release_names_the_labware_or_the_mover(error: Exception, code: str) -> None:
    runtime = _labware_raising("release_mover_hold", error)
    with pytest.raises(OperationError) as refused:
        await ReleaseMoverHoldOperation(runtime).run(ReleaseMoverHoldRequest(device_id="arm", reason="released"))
    assert (refused.value.surface_code(), refused.value.http_status) == (code, 404)
