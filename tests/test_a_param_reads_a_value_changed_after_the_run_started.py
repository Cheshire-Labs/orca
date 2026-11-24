"""`ctx.param` reads the variable store on every call, so a value changed during a run reaches the next read."""

import asyncio

import pytest

from orca.variables.errors import VariableValidationError
from orca.variables.variable_definition import VariableDefinition
from orca.variables.variable_store import VariableService, VariableStore
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.device_handle import ActionRequest

pytestmark = pytest.mark.asyncio

EXECUTION = "exec-1"
SUBMISSION = "sub-1"


def _service() -> VariableService:
    service = VariableService(VariableStore())
    service.register_workflow_definitions(
        "wf", {"speed": VariableDefinition(type="int", default=400, min=100, max=1000)},
    )
    service.create_execution(EXECUTION, "wf")
    return service


def _ctx(service: VariableService, submission_id: str | None = None) -> ActionContext:
    queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
    return ActionContext(
        device_name="shaker_1",
        action_queue=queue,
        assigned_labware={},
        variable_store=service,
        execution_id=EXECUTION,
        submission_id=submission_id,
    )


async def test_a_value_set_on_the_execution_is_read_by_the_next_param_call() -> None:
    service = _service()
    ctx = _ctx(service)
    assert await ctx.param("speed") == 400

    service.set("speed", 800, EXECUTION)

    assert await ctx.param("speed") == 800


async def test_a_value_set_on_the_submission_is_read_by_that_submission_s_next_call() -> None:
    service = _service()
    service.set_submission("speed", 500, EXECUTION, SUBMISSION)
    mine, other = _ctx(service, SUBMISSION), _ctx(service)
    assert (await mine.param("speed"), await other.param("speed")) == (500, 400)

    service.set_submission("speed", 700, EXECUTION, SUBMISSION)

    assert (await mine.param("speed"), await other.param("speed")) == (700, 400)


async def test_a_refused_change_leaves_the_next_read_on_the_old_value() -> None:
    service = _service()
    service.set_submission("speed", 500, EXECUTION, SUBMISSION)
    ctx = _ctx(service, SUBMISSION)

    with pytest.raises(VariableValidationError):
        service.set_submission("speed", 5000, EXECUTION, SUBMISSION)
    with pytest.raises(VariableValidationError):
        service.set("speed", 50, EXECUTION)

    assert await ctx.param("speed") == 500
    assert await _ctx(service).param("speed") == 400
