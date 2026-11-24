"""Typed-envelope wire-shape tests for SubmitExecutionOperation.

Pins the start-location and sim-hierarchy typed exception classes that
SubmitExecutionOperation surfaces as ``OperationError.typed(...)``:

- ``StartLocationsOccupiedError`` -> ``START_LOCATION_OCCUPIED`` (409)
- ``SpawnIncompatibleError`` -> ``SPAWN_INCOMPATIBLE`` (409)
- ``ReuseThreadCannotBeEntryError`` -> ``REUSE_THREAD_CANNOT_BE_ENTRY`` (409)
- ``RunModeMismatchError`` -> ``RUN_MODE_MISMATCH`` (409)

These exercise the legacy ``submit_workflow`` dispatch path (no groups,
STANDALONE, no operator_id).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations.protocol import OperationError
from orca.operations.submission import SubmitExecutionOperation
from orca.operations.submission_models import SubmitExecutionRequest
from orca.modes.run_modes import WorkflowRunMode
from orca.system.errors import DeviceInitializationError, DevicesNotReadyError
from orca.runtime.errors import (
    OtherRunModeActiveError,
    OccupiedSlot,
    RunModeMismatchError,
    StartLocationsOccupiedError,
    SubmissionToPausedExecutionError,
)
from orca.workflow_models.errors import ReuseThreadCannotBeEntryError, SpawnIncompatibleError


def _make_runtime(submit_workflow_side_effect: Exception) -> Any:
    runtime = MagicMock()
    runtime.submissions = MagicMock()
    runtime.submissions.submit_group = AsyncMock(
        side_effect=submit_workflow_side_effect,
    )
    return runtime


def _legacy_request() -> SubmitExecutionRequest:
    return SubmitExecutionRequest(
        workflow_name="simple_workflow",
        run_mode="CHECK_RUN",
    )


@pytest.mark.asyncio
async def test_start_locations_occupied_surfaces_typed_409_envelope() -> None:
    """The pre-submit start-location check raises with the typed
    occupied-slot list; the Operation surfaces it as 409
    ``START_LOCATION_OCCUPIED`` with the slot list under
    ``extras.occupied``."""
    slot = OccupiedSlot(
        position_id="bench_A1",
        existing_labware_id="lw-1",
        existing_labware_name="plate_X",
        existing_template_name="plate_96",
        source="unknown",
    )
    exc = StartLocationsOccupiedError(occupied=[slot])
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.http_status == 409
    assert raised.surface_code() == "start_location_occupied"
    assert raised.http_status == 409
    assert raised.extras == {
        "occupied": [
            {
                "position_id": "bench_A1",
                "existing_labware_id": "lw-1",
                "existing_labware_name": "plate_X",
                "existing_template_name": "plate_96",
                "source": "unknown",
            },
        ],
    }


@pytest.mark.asyncio
async def test_spawn_incompatible_surfaces_typed_409_envelope() -> None:
    """Wrong-template labware at the spawn location yields 409
    ``SPAWN_INCOMPATIBLE`` with location + expected/actual template
    names in extras."""
    exc = SpawnIncompatibleError(
        location="reagent_pad",
        expected_template="trough_25mL",
        actual_template="trough_100mL",
    )
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.http_status == 409
    assert raised.surface_code() == "spawn_incompatible"
    assert raised.http_status == 409
    assert raised.extras == {
        "location": "reagent_pad",
        "expected_template": "trough_25mL",
        "actual_template": "trough_100mL",
    }


@pytest.mark.asyncio
async def test_reuse_thread_cannot_be_entry_surfaces_typed_409_envelope() -> None:
    """Build-time check that a reuse-binding thread was registered as a
    workflow entry yields 409 ``REUSE_THREAD_CANNOT_BE_ENTRY`` with the
    offending thread name."""
    exc = ReuseThreadCannotBeEntryError(thread_name="reagent_journey")
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.http_status == 409
    assert raised.surface_code() == "reuse_thread_cannot_be_entry"
    assert raised.http_status == 409
    assert raised.extras == {"thread_name": "reagent_journey"}


@pytest.mark.asyncio
async def test_run_mode_mismatch_surfaces_typed_409_envelope() -> None:
    """JOIN_EXISTING submission against a different-mode execution yields
    409 ``RUN_MODE_MISMATCH`` with blocking-execution context."""
    exc = RunModeMismatchError(
        blocking_execution_id="exec-abc",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.CHECK_RUN,
        submitted_run_mode=WorkflowRunMode.LIVE,
    )
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.http_status == 409
    assert raised.surface_code() == "run_mode_mismatch"
    assert raised.http_status == 409
    assert raised.extras == {
        "blocking_execution_id": "exec-abc",
        "blocking_workflow_name": "smc_assay",
        "existing_run_mode": "CHECK_RUN",
        "submitted_run_mode": "LIVE",
    }


@pytest.mark.asyncio
async def test_submission_to_paused_execution_surfaces_typed_409_envelope() -> None:
    """JOIN_EXISTING submission targeting a paused execution yields 409
    ``submission_to_paused_execution`` with blocking-execution context, so a
    client can distinguish 'paused' from any other conflict without string
    matching (mirrors the run-mode-mismatch precedent, Decision C5)."""
    exc = SubmissionToPausedExecutionError(
        blocking_execution_id="exec-paused",
        blocking_workflow_name="smc_assay",
    )
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.http_status == 409
    assert raised.surface_code() == "submission_to_paused_execution"
    assert raised.http_status == 409
    assert raised.extras == {
        "blocking_execution_id": "exec-paused",
        "blocking_workflow_name": "smc_assay",
    }


@pytest.mark.asyncio
async def test_other_run_mode_active_surfaces_typed_409_envelope() -> None:
    """A submission in another run mode than a running execution yields 409
    ``OTHER_RUN_MODE_ACTIVE`` with the blocking execution + both run modes."""
    exc = OtherRunModeActiveError(
        blocking_execution_id="exec-live",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.LIVE,
        submitted_run_mode=WorkflowRunMode.SIM,
    )
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.http_status == 409
    assert raised.surface_code() == "other_run_mode_active"
    assert raised.http_status == 409
    assert raised.extras == {
        "blocking_execution_id": "exec-live",
        "blocking_workflow_name": "smc_assay",
        "existing_run_mode": "LIVE",
        "submitted_run_mode": "SIM",
    }


@pytest.mark.asyncio
async def test_a_run_refused_for_devices_that_are_not_ready_names_each_device() -> None:
    """A live device that did not come up refuses the run with 409 ``DEVICES_NOT_READY``."""
    exc = DevicesNotReadyError([DeviceInitializationError("pf400_1", ConnectionError("no answer"))])
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.http_status == 409
    assert raised.surface_code() == "devices_not_ready"
    assert raised.http_status == 409
    assert raised.extras == {"devices": {"pf400_1": "no answer"}}
