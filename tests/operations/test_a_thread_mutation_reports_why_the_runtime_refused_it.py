"""The thread mutation Operations, and aborting a manually paused thread's method, turn a refused runtime call into a typed OperationError."""

import textwrap
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from orca.daemon.app import InjectedSystem, create_app
from orca.operations.protocol import OperationError
from orca.operations.thread import (
    InsertActionOperation,
    RecoverThreadOperation,
    InsertMethodOperation,
    ReplaceActionOperation,
    ReplaceMethodOperation,
    SkipActionOperation,
    SkipMethodOperation,
)
from orca.operations.thread_models import (
    InsertActionRequest,
    RecoverThreadRequest,
    InsertMethodRequest,
    ReplaceActionRequest,
    ReplaceMethodRequest,
    SkipActionRequest,
    SkipMethodRequest,
)
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.facade_interfaces import IThreadFacade
from orca.runtime.runtime_interface import ISystemRuntime
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import MethodContext, MethodTemplate
from tests.mock import UniversalMockDevice

EXECUTION_ID = "exec-1"
THREAD_ID = "thread-1"
REASON = "operator asked for it"

_ACTION_CODE = textwrap.dedent("""
    from orca.devices.device_interfaces import IShaker
    from orca.sdk.labware import AnyLabwareTemplate

    @orca.action(device=topology.pool("the_pool"), inputs=[AnyLabwareTemplate()])
    async def ad_hoc(ctx):
        await ctx.device(IShaker).shake(duration=1, speed=100)
""")


async def _no_actions(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
    return
    yield


@dataclass(frozen=True)
class Case:
    name: str
    run: Callable[[ISystemRuntime], Awaitable[BaseModel]]
    facade_method: str


CASES: list[Case] = [
    Case(
        "skip_method",
        lambda rt: SkipMethodOperation(rt).run(SkipMethodRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, method_name="m", reason=REASON,
        )),
        "skip_method",
    ),
    Case(
        "abort_method",
        lambda rt: RecoverThreadOperation(rt).run(RecoverThreadRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, decision="ABORT_METHOD", reason=REASON,
        )),
        "abort_method",
    ),
    Case(
        "skip_action",
        lambda rt: SkipActionOperation(rt).run(SkipActionRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, action_command="shake", reason=REASON,
        )),
        "skip_action",
    ),
    Case(
        "insert_method",
        lambda rt: InsertMethodOperation(rt).run(InsertMethodRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, template_name="m",
            where="tail", reason=REASON,
        )),
        "insert_method",
    ),
    Case(
        "replace_method",
        lambda rt: ReplaceMethodOperation(rt).run(ReplaceMethodRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, target_name="old",
            template_name="m", reason=REASON,
        )),
        "replace_method",
    ),
    Case(
        "insert_action",
        lambda rt: InsertActionOperation(rt).run(InsertActionRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, action_code=_ACTION_CODE,
            where="tail", reason=REASON,
        )),
        "insert_action",
    ),
    Case(
        "replace_action",
        lambda rt: ReplaceActionOperation(rt).run(ReplaceActionRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, target_command="shake",
            action_code=_ACTION_CODE, reason=REASON,
        )),
        "replace_action",
    ),
]

CASE_PARAMS = [pytest.param(case, id=case.name) for case in CASES]


def _runtime_whose(facade_method: str, raises: Exception | None) -> ISystemRuntime:
    runtime = MagicMock(spec=ISystemRuntime)
    threads = MagicMock(spec=IThreadFacade)
    getattr(threads, facade_method).return_value = False
    getattr(threads, facade_method).side_effect = raises
    runtime.threads = threads
    runtime.get_execution.return_value.workflow_name = "wf"
    runtime.registry.get_method_template.return_value = MethodTemplate("m", _no_actions)
    device = UniversalMockDevice("shaker_1")
    runtime.system.get_resource_pool.return_value = ResourcePool("the_pool", [device])
    return runtime


async def _run(case: Case, raises: Exception | None) -> BaseModel:
    runtime = _runtime_whose(case.facade_method, raises)
    return await case.run(runtime)


@pytest.mark.parametrize("case", CASE_PARAMS)
async def test_a_call_the_runtime_accepts_reaches_the_thread_facade_once(case: Case) -> None:
    runtime = _runtime_whose(case.facade_method, None)

    await case.run(runtime)

    call = getattr(runtime.threads, case.facade_method)
    call.assert_awaited_once()
    assert call.await_args.args[:2] == (EXECUTION_ID, THREAD_ID)


@pytest.mark.parametrize("case", CASE_PARAMS)
async def test_a_missing_target_is_not_found_with_its_ids(case: Case) -> None:
    raised = KeyError("no thread 'thread-1' in execution 'exec-1'")
    with pytest.raises(OperationError) as caught:
        await _run(case, raised)
    error = caught.value
    assert error.http_status == 404
    assert error.message == "no thread 'thread-1' in execution 'exec-1'"
    assert error.extras == {"execution_id": EXECUTION_ID, "thread_id": THREAD_ID}
    assert error.__cause__ is raised


@pytest.mark.parametrize("case", CASE_PARAMS)
async def test_a_bad_request_is_invalid_input_with_the_runtime_text(case: Case) -> None:
    raised = ValueError("method 'm' is not pending")
    with pytest.raises(OperationError) as caught:
        await _run(case, raised)
    error = caught.value
    assert error.http_status == 422
    assert error.message == "method 'm' is not pending"
    assert error.extras is None
    assert error.__cause__ is raised


@pytest.mark.parametrize("case", CASE_PARAMS)
async def test_a_thread_in_the_wrong_state_is_a_conflict_with_the_runtime_text(case: Case) -> None:
    raised = RuntimeError("thread is not paused")
    with pytest.raises(OperationError) as caught:
        await _run(case, raised)
    error = caught.value
    assert error.http_status == 409
    assert error.message == "thread is not paused"
    assert error.extras is None
    assert error.__cause__ is raised


async def test_aborting_the_method_of_a_paused_thread_with_none_in_progress_is_a_conflict() -> None:
    runtime = _runtime_whose("abort_method", None)
    runtime.threads.get.return_value.current_method = None

    with pytest.raises(OperationError) as caught:
        await RecoverThreadOperation(runtime).run(RecoverThreadRequest(
            execution_id=EXECUTION_ID, thread_id=THREAD_ID, decision="ABORT_METHOD", reason=REASON,
        ))

    assert caught.value.http_status == 409
    assert caught.value.message == f"thread {THREAD_ID!r} has no method in progress to abort"
    runtime.threads.abort_method.assert_not_awaited()


@pytest_asyncio.fixture
async def skip_method_client(injected: InjectedSystem) -> AsyncIterator[AsyncClient]:
    app = create_app(injected=injected)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://daemon.test",
    ) as client:
        yield client


_SKIP_METHOD_BODY = {"method_name": "m", "reason": REASON}


@pytest.mark.parametrize(
    ("raised", "http_status", "code"),
    [
        pytest.param(KeyError("no such thread"), 404, "thread_not_found", id="key_error"),
        pytest.param(ValueError("bad"), 422, "validation_error", id="value_error"),
        pytest.param(RuntimeError("wrong state"), 409, "conflict", id="runtime_error"),
    ],
)
async def test_the_skip_method_route_sends_the_status_for_each_refusal(
    skip_method_client: AsyncClient, injected: InjectedSystem,
    monkeypatch: pytest.MonkeyPatch, raised: Exception, http_status: int, code: str,
) -> None:
    async def refuse(*args: str, **kwargs: str | bool | None) -> None:
        raise raised

    monkeypatch.setattr(injected.runtime.threads, "skip_method", refuse)
    response = await skip_method_client.post(
        f"/api/executions/{EXECUTION_ID}/threads/{THREAD_ID}/skip-method", json=_SKIP_METHOD_BODY,
    )
    assert response.status_code == http_status
    assert response.json()["detail"]["code"] == code
