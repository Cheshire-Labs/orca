"""POST /api/executions with no groups runs exactly as `submit_workflow` does.

The route sends every submission through `submissions.submit_group`. A submission that names no groups
must still leave the same submission, execution and events as the runtime's own `submit_workflow`.
"""

import asyncio
import re
from collections import Counter

import pytest

from orca.events.event_bus import EventBus
from orca.events.execution_context import ExecutionContext
from orca.modes.run_modes import WorkflowRunMode
from orca.operations.submission import SubmitExecutionOperation
from orca.operations.submission_models import SubmitExecutionRequest
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.package import build_system
from tests.daemon.daemon_test_fixture_topology import build_topology, build_workflow
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.setup_package import package_stores

pytestmark = pytest.mark.positions_bound_by_the_build

_TERMINAL = {"completed", "failed", "aborted", "stopped"}
_AN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}|[0-9a-f]{8}")


async def _runtime_that_records(events: list[str]) -> SystemRuntime:
    topology = build_topology()
    bus = EventBus()

    def record(event_name: str, context: ExecutionContext) -> None:
        events.append(_AN_ID.sub("<id>", event_name))

    bus.subscribe_all(record)
    build = await build_system(
        "no_groups", topology, package_stores(),
        workflow=build_workflow(topology), event_bus=bus, ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    return runtime


async def _finished(runtime: SystemRuntime, execution_id: str) -> str:
    async def poll() -> str:
        while (status := runtime.get_execution_status(execution_id).status) not in _TERMINAL:
            await asyncio.sleep(0.01)
        return status

    return await asyncio.wait_for(poll(), timeout=30.0)


@pytest.mark.asyncio
async def test_a_submission_with_no_groups_leaves_what_submit_workflow_leaves() -> None:
    events: list[str] = []
    runtime = await _runtime_that_records(events)
    try:
        record = await runtime.submit_workflow("simple_workflow", {}, mode=WorkflowRunMode.CHECK_RUN)
        [submitted] = runtime.submissions.list_submissions(execution_id=record.id)
        assert await _finished(runtime, record.id) == "completed"
        by_runtime = runtime.get_execution_detail(record.id)
        by_runtime_events = Counter(events)
        events.clear()

        answer = await SubmitExecutionOperation(runtime=runtime).run(
            SubmitExecutionRequest(workflow_name="simple_workflow", run_mode="CHECK_RUN"),
        )
        assert runtime.variables.get_all(answer.execution_id) == runtime.variables.get_all(record.id)
        assert await _finished(runtime, answer.execution_id) == "completed"
        by_route = runtime.get_execution_detail(answer.execution_id)

        assert (answer.group_count, answer.batch_mode, answer.run_mode, answer.operator_id) == (
            submitted.group_count, submitted.batch_mode, submitted.run_mode, submitted.operator_id,
        )
        assert (by_route.workflow_name, by_route.total_thread_count, by_route.completed_thread_count) == (
            by_runtime.workflow_name, by_runtime.total_thread_count, by_runtime.completed_thread_count,
        )
        assert Counter(events) == by_runtime_events
    finally:
        await runtime.shutdown(confirm=True)
