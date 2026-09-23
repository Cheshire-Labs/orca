"""Execution: a long-lived run of one WorkflowTemplate.

Wraps an ExecutingWorkflow. Owns submission lifecycle. Remains in ACCEPTING
until all known submissions complete and the runtime finalizes the execution.

T6b scope: single-submission Execution (one submission per execution).
T6c+ will extend to multi-submission executions keyed by workflow name.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.stall_detector import StallReport
from orca.runtime.submission import Submission
from orca.system.system_interface import ISystem
from orca.workflow_models.workflow_templates import WorkflowTemplate
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow


@dataclass
class Execution:
    """One long-lived run of one WorkflowTemplate.

    Owns an ExecutingWorkflow, the asyncio task that drives it, and the list
    of submissions accepted against it.
    """
    id: str
    workflow_name: str
    workflow: WorkflowTemplate = field(repr=False)
    system: ISystem = field(repr=False)
    task: asyncio.Task[None] = field(repr=False)
    phase: ExecutionPhase = ExecutionPhase.ACCEPTING
    error: str | None = None
    submissions: list[Submission] = field(default_factory=list)
    executing_workflow: ExecutingWorkflow | None = field(default=None, repr=False)
    workflow_attached: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    # Execution-level pause latch (orthogonal to ``phase``) gating new
    # submissions; ``abort_armed`` lets a second confirmed stop abort.
    paused_at: datetime | None = None
    abort_armed: bool = False

    # Threads started while this is set are born paused (with the reason
    # below), kept here so a stop landing before the workflow attaches is not lost.
    new_threads_held: bool = False
    new_threads_hold_reason: str = "manual"

    # Set by the stall detector's _handle_stall so waiters fail fast with a
    # typed error; cleared on operator resume (the stall episode ends).
    stall_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stall_report: StallReport | None = field(default=None, repr=False)

    @property
    def is_paused(self) -> bool:
        return self.paused_at is not None

    @property
    def pause_reason(self) -> str | None:
        """Who set the latch, or None when it is not set. The hold reason
        outlives a resume, so it only means anything while paused."""
        return self.new_threads_hold_reason if self.is_paused else None


@dataclass(frozen=True)
class StopOutcome:
    """Result of a two-phase stop request.

    ``armed`` -- the execution was paused and abort armed (call 1, a cold
    confirmed call, or a confirm that arrived while not yet armed).
    ``aborted`` -- the execution was aborted (a confirmed call on an armed,
    still-paused execution). The two are mutually exclusive.
    ``phase`` -- the execution phase after the request was applied; on an
    abort this is the accurate terminal value (ABORTED), not STOPPING.
    """
    armed: bool
    aborted: bool
    phase: ExecutionPhase
