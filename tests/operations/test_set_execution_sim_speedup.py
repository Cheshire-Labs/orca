"""The set-execution-sim-speedup operation maps the runtime's refusals to the wire."""

from unittest.mock import MagicMock

import pytest

from orca.operations.protocol import OperationError
from orca.operations.execution import SetExecutionSimSpeedupOperation
from orca.operations.execution_models import SetExecutionSimSpeedupRequest
from orca.runtime.errors import SimSpeedupNotApplicableError


def _runtime_that(raises: Exception | None = None, returns: float = 0.5) -> MagicMock:
    runtime = MagicMock()
    if raises is not None:
        runtime.set_execution_sim_speedup.side_effect = raises
    else:
        runtime.set_execution_sim_speedup.return_value = returns
    return runtime


async def test_a_change_answers_with_the_speedup_now_in_force() -> None:
    op = SetExecutionSimSpeedupOperation(runtime=_runtime_that(returns=2.0))
    resp = await op.run(SetExecutionSimSpeedupRequest(execution_id="e1", sim_speedup=2.0))
    assert resp.execution_id == "e1"
    assert resp.sim_speedup == 2.0
    assert resp.message == "2x faster than real time"


async def test_a_live_execution_is_a_422_with_its_own_wire_code() -> None:
    op = SetExecutionSimSpeedupOperation(
        runtime=_runtime_that(SimSpeedupNotApplicableError("a LIVE run is paced by its hardware")),
    )
    with pytest.raises(OperationError) as info:
        await op.run(SetExecutionSimSpeedupRequest(execution_id="e1", sim_speedup=1.0))
    assert info.value.surface_code() == "sim_speedup_not_applicable"
    assert info.value.http_status == 422
    assert info.value.extras is not None
    assert info.value.extras["reason"] == "a LIVE run is paced by its hardware"


async def test_an_unknown_execution_is_not_found() -> None:
    op = SetExecutionSimSpeedupOperation(runtime=_runtime_that(KeyError("nope")))
    with pytest.raises(OperationError) as info:
        await op.run(SetExecutionSimSpeedupRequest(execution_id="nope", sim_speedup=1.0))
    assert info.value.surface_code() == "execution_not_found"


async def test_a_finished_execution_is_a_conflict() -> None:
    op = SetExecutionSimSpeedupOperation(runtime=_runtime_that(RuntimeError("finished")))
    with pytest.raises(OperationError) as info:
        await op.run(SetExecutionSimSpeedupRequest(execution_id="e1", sim_speedup=1.0))
    assert info.value.surface_code() == "conflict"


@pytest.mark.parametrize("bad", [-1.0, 0.0, 0.05, 1001.0])
def test_a_speedup_outside_its_range_is_refused_at_the_request(bad: float) -> None:
    with pytest.raises(ValueError):
        SetExecutionSimSpeedupRequest(execution_id="e1", sim_speedup=bad)
