"""A JOIN_EXISTING submission with bad inputs is refused before the run it joins records it."""

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.variables import VariableDefinition
from orca.variables.errors import SubmissionInputsRefusedError
from orca.workflow_models.submission import BatchMode
from tests.memory_activity import memory_runtime
from tests.test_helpers import execution_outcome
from tests.test_join_existing_injection_close_race import _build_gated_lineage_system, _FeederGate, _sample_group


@pytest.mark.asyncio
async def test_a_join_naming_an_undeclared_variable_is_refused_and_not_recorded() -> None:
    gate = _FeederGate()
    system, workflow, event_bus = await _build_gated_lineage_system(gate)
    system.variable_store.register_workflow_definitions(workflow.name, {"speed": VariableDefinition(type="int", default=1)})
    runtime = await memory_runtime(system, event_bus=event_bus)
    await runtime.start()
    try:
        first = await runtime.submit(
            workflow, groups=[_sample_group()], batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.CHECK_RUN,
        )
        await gate.reached.wait()

        with pytest.raises(SubmissionInputsRefusedError, match="colour is not a variable"):
            await runtime.submit(
                workflow, groups=[_sample_group()], variables={"colour": "red"},
                batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.CHECK_RUN,
            )

        assert [s.id for s in runtime._executions[first.execution_id].submissions] == [first.id]
        gate.release.set()
        assert (await execution_outcome(runtime, first, timeout=30.0)).status == "completed"
    finally:
        await runtime.shutdown()
