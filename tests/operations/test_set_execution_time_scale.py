"""The set-execution-time-scale operation maps the runtime's refusals to the wire."""

from unittest.mock import MagicMock

import pytest

from orca.operations._protocol import OperationError
from orca.operations.execution import SetExecutionTimeScaleOperation
from orca.operations.execution_models import SetExecutionTimeScaleRequest
from orca.runtime.runtime_interface import SimTimeScaleNotApplicableError


def _runtime_that(raises: Exception | None = None, returns: float = 0.5) -> MagicMock:
    runtime = MagicMock()
    if raises is not None:
        runtime.set_execution_time_scale.side_effect = raises
    else:
        runtime.set_execution_time_scale.return_value = returns
    return runtime


async def test_a_change_answers_with_the_scale_now_in_force() -> None:
    op = SetExecutionTimeScaleOperation(runtime=_runtime_that(returns=0.5))
    resp = await op.run(SetExecutionTimeScaleRequest(execution_id="e1", sim_time_scale=0.5))
    assert resp.execution_id == "e1"
    assert resp.sim_time_scale == 0.5
    assert resp.message == "2x faster than real time"


async def test_a_live_execution_is_a_422_with_its_own_wire_code() -> None:
    op = SetExecutionTimeScaleOperation(
        runtime=_runtime_that(SimTimeScaleNotApplicableError("a LIVE run is paced by its hardware")),
    )
    with pytest.raises(OperationError) as info:
        await op.run(SetExecutionTimeScaleRequest(execution_id="e1", sim_time_scale=1.0))
    assert info.value.wire_code == "SIM_TIME_SCALE_NOT_APPLICABLE"
    assert info.value.status_code == 422
    assert info.value.extras is not None
    assert info.value.extras["reason"] == "a LIVE run is paced by its hardware"


async def test_an_unknown_execution_is_not_found() -> None:
    op = SetExecutionTimeScaleOperation(runtime=_runtime_that(KeyError("nope")))
    with pytest.raises(OperationError) as info:
        await op.run(SetExecutionTimeScaleRequest(execution_id="nope", sim_time_scale=1.0))
    assert info.value.code.value == "not_found"


async def test_a_finished_execution_is_a_conflict() -> None:
    op = SetExecutionTimeScaleOperation(runtime=_runtime_that(RuntimeError("finished")))
    with pytest.raises(OperationError) as info:
        await op.run(SetExecutionTimeScaleRequest(execution_id="e1", sim_time_scale=1.0))
    assert info.value.code.value == "conflict"


def test_a_negative_scale_is_refused_at_the_request() -> None:
    with pytest.raises(ValueError):
        SetExecutionTimeScaleRequest(execution_id="e1", sim_time_scale=-1.0)
