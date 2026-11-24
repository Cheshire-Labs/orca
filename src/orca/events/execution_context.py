from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class WorkflowExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # `execution_id` is the UUID returned by SystemRuntime.submit_workflow and
    # used in every external surface (REST, CLI, events). It is also the id of
    # the WorkflowInstance that runs this execution (they are the same concept;
    # one submission = one workflow instance).
    execution_id: str
    workflow_name: str


class ThreadExecutionContext(WorkflowExecutionContext):
    thread_id: str
    thread_name: str
    template_name: str
    group_id: Optional[str] = None
    # Populated only on the PAUSED transition that follows an action error
    # so THREAD.<id>.PAUSED events carry the cause, not just the fact of a
    # pause. None for every other transition (CREATED, RUNNING, manual pause).
    pause_reason: Optional[str] = None
    last_error: Optional[str] = None


class MethodExecutionContext(WorkflowExecutionContext):
    method_id: Optional[str]
    method_name: Optional[str]
    thread_id: Optional[str] = None
    thread_name: Optional[str] = None
    participating_thread_ids: tuple[str, ...] = ()


class LocationActionExecutionContext(MethodExecutionContext):
    action_id: str
    action_status: str
    action_name: Optional[str] = None


class MoveActionExecutionContext(ThreadExecutionContext):
    action_id: str
    action_status: str
    action_name: Optional[str] = None


class SubmissionExecutionContext(WorkflowExecutionContext):
    """Context for SUBMISSION.* lifecycle events.

    execution_id is the workflow_instance_id of the execution the
    submission belongs to. Runtime listeners use (execution_id,
    submission_id) for tracking.
    """
    submission_id: str
    group_count: int = 0
    reason: Optional[str] = None


class GroupLifecycleContext(WorkflowExecutionContext):
    """Context for GROUP.* lifecycle events."""
    submission_id: str
    group_id: str


class ExecutionLifecycleContext(WorkflowExecutionContext):
    """Context for EXECUTION.* lifecycle events."""
    reason: Optional[str] = None


class ExecutionSimSpeedupContext(WorkflowExecutionContext):
    """Context for EXECUTION.<id>.SIM_SPEEDUP_CHANGED: the sim speed-up now in force."""
    sim_speedup: float


class OperatorInstructionContext(WorkflowExecutionContext):
    """Context for OPERATOR.INSTRUCTION events raised by ctx.manual_step.

    Carries the operator-facing instruction plus the step_id used to
    confirm it (the matching OPERATOR.CONFIRM.<step_id> channel).
    """
    thread_id: Optional[str] = None
    instruction: str
    step_id: str


class ManualInterventionContext(ThreadExecutionContext):
    """Context for AWAITING_MANUAL_PLACE / AWAITING_MANUAL_REMOVE thread
    status events, enriched with the labware identity and the target slot
    so an operator-notification waiter can act without a second lookup.
    """
    labware_id: Optional[str] = None
    labware_name: Optional[str] = None
    labware_template_name: Optional[str] = None
    target_location: Optional[str] = None


class CustomEventContext(WorkflowExecutionContext):
    """Context for CUSTOM.* events published by ctx.emit.

    ``event_name`` is the author's name, which is also the bus event's
    entity_id: the event-name rule allows no dots in it.
    ``thread_id`` names the single owning thread when the emit has one
    (thread-level emits, and action-level emits from a single-thread action);
    None when no single thread owns it, such as a shared or co-thread action.
    """
    thread_id: Optional[str] = None
    event_name: str
    value: Optional[str] = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


class IncidentContext(BaseModel):
    """Context for INCIDENT.* events emitted when an incident is recorded.

    Summary fields only; the full typed detail stays queryable via the
    incidents surface (incidents_get / GET /api/incidents) keyed by
    incident_id.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    category: str
    severity: str
    message: str
    recovery_action: str
    execution_id: Optional[str] = None
    thread_id: Optional[str] = None


class DeviceFaultContext(BaseModel):
    """Context for DEVICE.<name>.FAULTED / .FAULT_CLEARED events.

    A fault was state on the device row and nothing else, so no observer could
    see one arrive: not a UI, not the intervention long-poll, not the event
    archive. It is the condition that stops every workflow command on that
    device, which makes it exactly the kind of thing the stream is for.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_name: str
    cleared: bool
    command: Optional[str] = None
    outcome: Optional[str] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    may_still_be_moving: bool = False
    message: Optional[str] = None
    execution_id: Optional[str] = None


class DeviceCommandContext(BaseModel):
    """Context for COMMAND.<device>.STARTED / .COMPLETED / .FAILED: one device command, whoever sent it.

    `command_id` is the wire command's id, which the driver's own events carry as their call id.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str
    device_name: str
    command: str
    params: dict[str, JsonValue]
    # None for a command refused before it reached a device.
    mode: Optional[str] = None
    execution_id: Optional[str] = None
    # The thread that sent a run's command; None for a command no thread sent, such as an operator's.
    thread_id: Optional[str] = None
    thread_name: Optional[str] = None
    # How many times faster than real time the sender's run goes; None when no run paces the command.
    sim_speedup: Optional[float] = None
    # Seconds the engine budgets for the command at real-time pace, which a simulator waits out; None when it budgets none.
    nominal_seconds: Optional[float] = None
    result: JsonValue = None
    outcome: Optional[str] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    duration_ms: Optional[float] = None


class LabwareRecordContext(BaseModel):
    """Context for LABWARE.<id>.SAVED / .MOVED / .CLEARED: the labware record changed, so a read of it is stale."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_id: str
    position_id: Optional[str] = None


class DeviceConnectionContext(BaseModel):
    """Context for DEVICE.<name>.CONNECTED / .DISCONNECTED."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_name: str
    client_id: Optional[str] = None


ExecutionContext = Union[
    WorkflowExecutionContext,
    ThreadExecutionContext,
    MethodExecutionContext,
    LocationActionExecutionContext,
    MoveActionExecutionContext,
    SubmissionExecutionContext,
    GroupLifecycleContext,
    ExecutionLifecycleContext,
    ExecutionSimSpeedupContext,
    OperatorInstructionContext,
    ManualInterventionContext,
    CustomEventContext,
    IncidentContext,
    DeviceFaultContext,
]

class HostedEventContext(BaseModel):
    """Context of an event a hosted server adds to the stream; its fields and their contract are that server's."""
    model_config = ConfigDict(frozen=True, extra="allow")


# What a RuntimeEvent can carry: a workflow's own contexts, the facts a server sends from outside any workflow,
# and a hosted server's own events.
EventContext = Union[
    ExecutionContext,
    DeviceCommandContext,
    LabwareRecordContext,
    DeviceConnectionContext,
    HostedEventContext,
]
