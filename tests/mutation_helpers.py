"""Shared test helpers for mutation tests."""

from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.status_enums import LabwareThreadStatus
from tests.test_helpers import wait_for_paused_threads, wait_for_runtime_condition


async def wait_for_threads(
    runtime: SystemRuntime, execution_id: str, timeout: float = 5.0
) -> list[ThreadSnapshot]:
    """Wait until at least one thread appears in the execution."""
    await wait_for_runtime_condition(
        runtime,
        lambda: bool(runtime.list_threads(execution_id)),
        timeout=timeout,
        message="No threads registered within timeout",
    )
    return runtime.list_threads(execution_id)


async def pause_and_wait(
    runtime: SystemRuntime, execution_id: str, thread_id: str, timeout: float = 5.0
) -> None:
    """Request pause and wait until the thread reaches PAUSED status."""
    runtime.pause_thread(execution_id, thread_id)

    def _thread_paused() -> bool:
        for entry in runtime._executions.values():
            for t in entry.system.executing_threads:
                if t.id == thread_id and t.status == LabwareThreadStatus.PAUSED:
                    return True
        return False

    await wait_for_runtime_condition(
        runtime,
        _thread_paused,
        timeout=timeout,
        message=f"Thread {thread_id} did not reach PAUSED within timeout",
    )


async def wait_for_paused(
    runtime: SystemRuntime, execution_id: str, timeout: float = 10.0
) -> str:
    """Wait until any thread in the execution is PAUSED (error or manual). Return its id."""
    paused = await wait_for_paused_threads(
        runtime, execution_id, count=1, timeout=timeout
    )
    return paused[0].id
