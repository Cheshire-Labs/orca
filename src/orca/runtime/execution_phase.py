"""The lifecycle phase of an execution, on its own so the wire can read it.

Lives apart from `execution.py` because that module pulls in the workflow
engine, and everything that only needs the phase name would pay for it.
"""

from enum import Enum


class ExecutionPhase(str, Enum):
    """Lifecycle phase of an execution.

    str-backed so CLI/API string equality (`phase == "completed"`) keeps
    working alongside idiomatic `phase is ExecutionPhase.COMPLETED`.

    Lifecycle:
    - ACCEPTING: running and open to additional grouped submissions via
      mid-run injection (runtime.submit with groups against the same
      workflow_name).
    - DRAINING: runtime.close_execution has been invoked. The runtime
      refuses new grouped submits for this workflow_name; in-flight threads
      continue until terminal.
    - STOPPING: stop_execution() was called and the task was cancelled.
      Threads are cooperatively draining; the active-thread count may
      still be > 0 for seconds until cancellation propagates through
      their stop-event polls. Non-terminal; the task-done callback
      transitions STOPPING -> ABORTED once the task fully unwinds.
    - COMPLETED / FAILED / ABORTED: terminal.
    """
    ACCEPTING = "accepting"
    DRAINING = "draining"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
