"""A method run on its own is owned by a labware its workflow does not reuse, whatever order the labware is named in.

A reused labware cannot own the method, because its thread cannot start the run. A run that names only reused
labware is refused.
"""

import asyncio

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import NoLabwareCanOwnTheMethodError
from orca.runtime.system_runtime import ExecutionState
from tests.memory_activity import memory_runtime
from tests.runtime.test_resident_serves_a_second_execution import _RESERVOIR_SITE, _build_resident_system
from tests.test_helpers import run_to_quiescence

pytestmark = pytest.mark.positions_bound_by_the_build


async def test_the_reused_labware_named_first_does_not_own_the_method() -> None:
    build = await _build_resident_system()
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        placed = await runtime.submit(build.system.get_workflow_template("resident_wf"), mode=WorkflowRunMode.CHECK_RUN)
        await run_to_quiescence(runtime, placed.execution_id, timeout=60.0)

        record = await runtime.submit_method(
            "resident_wf", "add",
            {"reservoir": _RESERVOIR_SITE, "sample": "pad"},
            {"reservoir": _RESERVOIR_SITE, "sample": "pad"},
            mode=WorkflowRunMode.CHECK_RUN,
        )
        try:
            final = await asyncio.wait_for(runtime.wait(record.id), timeout=20.0)
        except TimeoutError:
            raise AssertionError([(t.name, t.status, t.pause_site, t.last_error) for t in runtime.get_execution_detail(record.id).threads])

        assert final.status is ExecutionState.COMPLETED, final
    finally:
        await runtime.shutdown(confirm=True)


async def test_a_run_over_only_reused_labware_is_refused() -> None:
    build = await _build_resident_system()
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        with pytest.raises(NoLabwareCanOwnTheMethodError, match="reservoir"):
            await runtime.submit_method(
                "resident_wf", "add", {"reservoir": _RESERVOIR_SITE}, {"reservoir": _RESERVOIR_SITE},
                mode=WorkflowRunMode.CHECK_RUN,
            )
    finally:
        await runtime.shutdown(confirm=True)
