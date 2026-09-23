"""Typed-envelope wire-shape tests for SubmitExecutionOperation.

Pins the start-location and sim-hierarchy typed exception classes that
SubmitExecutionOperation surfaces as ``OperationError.typed(...)``:

- ``LiveSubmissionWithSimOverridesUnacknowledgedError`` ->
  ``LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED`` (422)
- ``StartLocationsOccupiedError`` -> ``START_LOCATION_OCCUPIED`` (409)
- ``SpawnIncompatibleError`` -> ``SPAWN_INCOMPATIBLE`` (409)
- ``ReuseThreadCannotBeEntryError`` -> ``REUSE_THREAD_CANNOT_BE_ENTRY`` (409)
- ``RunModeMismatchError`` -> ``RUN_MODE_MISMATCH`` (409)

These exercise the legacy ``submit_workflow`` dispatch path (no groups,
STANDALONE, no operator_id / deployment_profile / acknowledge_warnings).
The multi-group path's typed catches are pinned by
``test_routes_submit_envelope.py::test_post_submissions_live_sim_overrides_route_returns_typed_422``.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
from orca.operations.submission import SubmitExecutionOperation
from orca.operations.submission_models import SubmitExecutionRequest
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import (
    ConcurrentLiveSimRefusedError,
    LiveSubmissionWithSimOverridesUnacknowledgedError,
    OccupiedSlot,
    ReuseThreadCannotBeEntryError,
    RunModeMismatchError,
    SpawnIncompatibleError,
    StartLocationsOccupiedError,
    SubmissionToPausedExecutionError,
)


def _make_runtime(submit_workflow_side_effect: Exception) -> Any:
    runtime = MagicMock()
    runtime.submit_workflow = AsyncMock(side_effect=submit_workflow_side_effect)
    runtime.submissions = MagicMock()
    runtime.submissions.submit_group = AsyncMock(
        side_effect=submit_workflow_side_effect,
    )
    return runtime


def _legacy_request() -> SubmitExecutionRequest:
    return SubmitExecutionRequest(
        workflow_name="simple_workflow",
        run_mode="PURE_SIM",
    )


@pytest.mark.asyncio
async def test_live_sim_overrides_surfaces_typed_422_envelope() -> None:
    """LIVE submission with unacknowledged sim_override device produces
    the typed 422 envelope on the Operation surface (binder translates
    to HTTPException; the Operation pins the wire_code + status_code +
    extras shape)."""
    exc = LiveSubmissionWithSimOverridesUnacknowledgedError(
        devices=[
            ("shaker1", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM),
            ("centrifuge2", WorkflowRunMode.DEVICE_SIM, WorkflowRunMode.DEVICE_SIM),
        ],
    )
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.code is OperationErrorCode.INVALID_INPUT
    assert raised.wire_code == "LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED"
    assert raised.status_code == 422
    assert raised.extras == {
        "devices": [
            {
                "name": "shaker1",
                "sim_override": "PURE_SIM",
                "resolved_mode": "PURE_SIM",
            },
            {
                "name": "centrifuge2",
                "sim_override": "DEVICE_SIM",
                "resolved_mode": "DEVICE_SIM",
            },
        ],
    }


@pytest.mark.asyncio
async def test_start_locations_occupied_surfaces_typed_409_envelope() -> None:
    """The pre-submit start-location check raises with the typed
    occupied-slot list; the Operation surfaces it as 409
    ``START_LOCATION_OCCUPIED`` with the slot list under
    ``extras.occupied``."""
    slot = OccupiedSlot(
        position_id="bench_A1",
        existing_labware_name="plate_X",
        existing_template_name="plate_96",
        source="unknown",
    )
    exc = StartLocationsOccupiedError(occupied=[slot])
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "start_location_occupied"
    assert raised.status_code == 409
    assert raised.extras == {
        "occupied": [
            {
                "position_id": "bench_A1",
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
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "spawn_incompatible"
    assert raised.status_code == 409
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
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "reuse_thread_cannot_be_entry"
    assert raised.status_code == 409
    assert raised.extras == {"thread_name": "reagent_journey"}


@pytest.mark.asyncio
async def test_run_mode_mismatch_surfaces_typed_409_envelope() -> None:
    """JOIN_EXISTING submission against a different-mode execution yields
    409 ``RUN_MODE_MISMATCH`` with blocking-execution context."""
    exc = RunModeMismatchError(
        blocking_execution_id="exec-abc",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.PURE_SIM,
        submitted_run_mode=WorkflowRunMode.LIVE,
    )
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "RUN_MODE_MISMATCH"
    assert raised.status_code == 409
    assert raised.extras == {
        "blocking_execution_id": "exec-abc",
        "blocking_workflow_name": "smc_assay",
        "existing_run_mode": "PURE_SIM",
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
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "submission_to_paused_execution"
    assert raised.status_code == 409
    assert raised.extras == {
        "blocking_execution_id": "exec-paused",
        "blocking_workflow_name": "smc_assay",
    }


@pytest.mark.asyncio
async def test_concurrent_live_sim_refused_surfaces_typed_409_envelope() -> None:
    """A DEVICE_SIM submission while a LIVE execution is alive (or vice
    versa) yields 409 ``CONCURRENT_LIVE_SIM_REFUSED`` with the blocking
    execution + both run modes."""
    exc = ConcurrentLiveSimRefusedError(
        blocking_execution_id="exec-live",
        blocking_workflow_name="smc_assay",
        existing_run_mode=WorkflowRunMode.LIVE,
        submitted_run_mode=WorkflowRunMode.DEVICE_SIM,
    )
    op = SubmitExecutionOperation(runtime=_make_runtime(exc))

    with pytest.raises(OperationError) as exc_info:
        await op.run(_legacy_request())

    raised = exc_info.value
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "CONCURRENT_LIVE_SIM_REFUSED"
    assert raised.status_code == 409
    assert raised.extras == {
        "blocking_execution_id": "exec-live",
        "blocking_workflow_name": "smc_assay",
        "existing_run_mode": "LIVE",
        "submitted_run_mode": "DEVICE_SIM",
    }
