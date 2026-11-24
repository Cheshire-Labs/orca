"""Workflow run modes, and what one device runs as under a run mode.

Lives in its own module so foundational layers (`orca.resource_models.devices`,
`orca.resource_models.transporter`) can read it without importing the heavier
`orca.runtime.status_models` graph.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum

from orca.modes.device_modes import DeviceMode


class WorkflowRunMode(str, Enum):
    """Workflow run mode controlling sim vs wire vs live dispatch.

    CHECK_RUN: every device runs its simulator on an orca-client inside Orca; no lab PC, unpaced.
    SIM: every device runs its simulator on its orca-client.
    LIVE: each device runs as its own sim or live setting.
    """

    CHECK_RUN = "CHECK_RUN"
    SIM = "SIM"
    LIVE = "LIVE"


# Per-async-task run mode for dispatch resolution.
#
# Seeded at the execution entrypoint, `SystemRuntime._run_workflow`, from the
# submission's stamped `run_mode`, then re-seeded per-thread by
# `ExecutingLabwareThread.start()`. asyncio.Task inherits a snapshot of
# the parent context at creation, so child tasks (snapshot reads
# inside an execution, registry walks under a workflow task) see the
# seed automatically.
#
# No default: a read outside a run passes `OUT_OF_RUN_BASE` as its fallback.
current_run_mode: ContextVar[WorkflowRunMode] = ContextVar("current_run_mode")


# The execution id of the running thread, seeded at thread start next to
# `current_run_mode`. Read by the remote drivers' `_send` so the gateway
# controller can tell a workflow command (engine-bounded) from an ad-hoc one
# (gateway fail-fast timer). None outside a thread = ad-hoc.
current_execution_id: ContextVar[str | None] = ContextVar(
    "current_execution_id", default=None,
)


@dataclass(frozen=True)
class RunningThread:
    id: str
    name: str


# The thread whose commands run in this context, seeded at thread start. None for a command no thread sent.
current_thread: ContextVar[RunningThread | None] = ContextVar("current_thread", default=None)


def device_run_mode(run_mode: WorkflowRunMode, setting: DeviceMode) -> WorkflowRunMode:
    """A check run or a sim run decides for every device; a live run uses each device's own setting."""
    if run_mode is WorkflowRunMode.LIVE:
        return WorkflowRunMode(setting)
    return run_mode


OUT_OF_RUN_BASE: WorkflowRunMode = WorkflowRunMode.LIVE
"""The base mode a device resolves against outside a run.

Under LIVE each device runs as its own sim or live setting, so a device set to
sim never reaches hardware. A check run is a whole run with its own runtime, so
nothing outside a run ever resolves to CHECK_RUN.
"""


BaseMode = Callable[[], WorkflowRunMode]
"""What a device resolves against when no run mode is seeded: the active run's mode, else `OUT_OF_RUN_BASE`."""


def out_of_run() -> WorkflowRunMode:
    return OUT_OF_RUN_BASE


@contextmanager
def mode_scope(base: WorkflowRunMode) -> Iterator[None]:
    """Seed `current_run_mode` for one call: 'this is the world I mean'.

    Device dispatch under the scope resolves each device's own setting against
    `base`. The prior value is restored on exit,
    so an operator verb cannot leak its base into unrelated reads.
    """
    token = current_run_mode.set(base)
    try:
        yield
    finally:
        current_run_mode.reset(token)


__all__ = [
    "BaseMode",
    "OUT_OF_RUN_BASE",
    "RunningThread",
    "WorkflowRunMode",
    "current_execution_id",
    "current_run_mode",
    "current_thread",
    "device_run_mode",
    "mode_scope",
    "out_of_run",
]
