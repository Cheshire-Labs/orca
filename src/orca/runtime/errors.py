"""Errors the runtime and its facades raise."""

from typing import Literal

from pydantic import BaseModel

from orca.runtime.status_models import (
    WorkflowRunMode,
)


class TopologyCollisionError(Exception):
    """Raised when a connected gateway's interfaces do not satisfy the topology contract.

    The safety contract is interface superset: a connected device's
    advertised interfaces MUST be a superset of the topology-declared
    interfaces (the methods the workflow will dispatch). Missing
    interfaces would crash the workflow at the wire. ``DeviceRegistryImpl.get``
    raises this error so the runtime fails loud rather than dispatching against
    a contract the wire cannot honor; the device list answers the row with the
    mismatch instead, so one device cannot hide the rest. Both servers answer
    it as ``device_interface_mismatch`` (409). It is not a ``RuntimeError``,
    so a route's generic ``RuntimeError`` catch cannot answer it as ``conflict``.

    Kind drift (different ``kind`` strings on either side) is advisory:
    multiple kind labels may satisfy the same interface contract. The
    runtime logs a warning for kind drift at start and only raises this
    error on interface contract breaks.
    """

    def __init__(self, device_name: str, message: str) -> None:
        super().__init__(message)
        self.device_name = device_name


# -- Submit-time run-mode validation ----------------------------------------


class RunModeRequiredError(ValueError):
    """Raised when `SystemRuntime.submit` is called without a `mode=` value.

    Sim-hierarchy v3.4 makes `Submission.run_mode` required. The operator
    must declare CHECK_RUN / SIM / LIVE at submit time; there is no
    deployment-level fallback.
    """

    def __init__(self) -> None:
        super().__init__(
            "Submission requires a `run_mode` (CHECK_RUN, SIM, or LIVE). "
            "No deployment-level default is applied at submit time."
        )


class SimSpeedupNotApplicableError(ValueError):
    """Raised when a sim speed-up is given where none can apply.

    Only a sim run is paced. LIVE is paced by its hardware and a check run never
    waits, so a speed-up on either is a contradiction, not a setting. A JOIN_EXISTING
    submission cannot carry one either: the speed-up belongs to the execution it
    joins, and changing that execution's own speed-up is the way. Shape refusal -> 422.
    """

    def __init__(self, reason: str, execution_id: str | None = None) -> None:
        self.reason = reason
        self.execution_id = execution_id
        super().__init__(f"sim_speedup does not apply: {reason}")


class DeploymentProfileNotFoundError(KeyError):
    """Raised when a submission names a deployment profile nobody saved."""

    def __init__(self, profile_name: str) -> None:
        self.profile_name = profile_name
        super().__init__(f"deployment profile {profile_name!r} not found")


class SubmissionToPausedExecutionError(RuntimeError):
    """Raised when a JOIN_EXISTING submission targets a paused execution.

    A paused execution is still ACCEPTING but its pause latch gates new work,
    so a join is refused rather than queued (queuing would hide the paused
    state from the operator). Resume the execution to accept joins, or submit
    STANDALONE to start a fresh execution. Engine-state refusal -> 409, like
    ``RunModeMismatchError``.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        super().__init__(
            f"Execution {blocking_execution_id!r} for workflow "
            f"{blocking_workflow_name!r} is paused and not accepting "
            f"submissions. Resume it to accept JOIN_EXISTING submissions, or "
            f"resubmit as STANDALONE to start a new execution."
        )


class SubmissionBlockedByOrphanedBacklogError(RuntimeError):
    """Raised when a JOIN_EXISTING submission targets an execution with a
    quarantined (orphaned-backlog) slot.

    A BATCHABLE slot key collapses the submission component, so a join would
    route the NEW submission's contributions straight into the quarantined
    slot, where the accept-partial resume would silently discard them.
    Engine-state refusal -> 409, like ``SubmissionToPausedExecutionError``.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        super().__init__(
            f"Execution {blocking_execution_id!r} for workflow "
            f"{blocking_workflow_name!r} has an orphaned backlog (a receiver "
            f"died still owing contributions) and is not accepting "
            f"JOIN_EXISTING submissions. Resume the execution to accept the "
            f"partial fill first, or resubmit as STANDALONE to start a new "
            f"execution."
        )


class RunModeMismatchError(RuntimeError):
    """Raised when a JOIN_EXISTING submission's run_mode differs from the live execution.

    Every submission in
    one execution must share the same `WorkflowRunMode` because the
    execution's running threads share a single per-task ContextVar
    chain; mixing modes would let two threads in the same execution
    dispatch against different drivers. STANDALONE submissions are
    unaffected because each gets its own fresh execution with its own
    ContextVar seed.

    Carries the blocking execution's id, the existing run_mode, and the
    submitted run_mode so callers can render an actionable error.
    Multi-mode-per-execution would require dispatch rewiring and is
    explicitly deferred.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
        existing_run_mode: WorkflowRunMode,
        submitted_run_mode: WorkflowRunMode,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        self.existing_run_mode = existing_run_mode
        self.submitted_run_mode = submitted_run_mode
        super().__init__(
            f"JOIN_EXISTING submission with run_mode="
            f"{submitted_run_mode.name} cannot join execution "
            f"{blocking_execution_id!r} (workflow "
            f"{blocking_workflow_name!r}), which is running under "
            f"run_mode={existing_run_mode.name}. Resubmit with the "
            f"matching run_mode, or use batch_mode=STANDALONE to start "
            f"a fresh execution."
        )


class OtherRunModeActiveError(RuntimeError):
    """Raised when a submission's run mode differs from an execution still running.

    A system runs in one run mode at a time. LIVE and SIM share the
    orca-client device connections, so a run in another mode waits until
    the running one ends.
    `RunModeMismatchError` is the same rule for a JOIN_EXISTING submission
    into one execution; this is the rule across executions. Maps to 409.
    """

    def __init__(
        self,
        blocking_execution_id: str,
        blocking_workflow_name: str,
        existing_run_mode: WorkflowRunMode,
        submitted_run_mode: WorkflowRunMode,
    ) -> None:
        self.blocking_execution_id = blocking_execution_id
        self.blocking_workflow_name = blocking_workflow_name
        self.existing_run_mode = existing_run_mode
        self.submitted_run_mode = submitted_run_mode
        super().__init__(
            f"Cannot start a {submitted_run_mode.name} execution while "
            f"execution {blocking_execution_id!r} (workflow "
            f"{blocking_workflow_name!r}) is running under "
            f"{existing_run_mode.name}. A system runs in one run mode at a "
            f"time. Wait for the running execution to end, then resubmit."
        )


class DeckLayoutRequiredError(RuntimeError):
    """Raised when a submission has unconfigured deck layouts.

    Every ``LiquidHandler`` in the topology must declare a non-None
    ``deck_layout`` and that name must resolve to an entry in its
    ``deck_layout_store`` before submit, in every run mode.

    ``missing_declaration`` names handlers whose constructor had
    ``deck_layout=None``. ``unresolved_layout`` names handlers whose
    declared layout could not be found in the per-device store.
    """

    def __init__(
        self,
        mode: "WorkflowRunMode",
        missing_declaration: list[str],
        unresolved_layout: list[tuple[str, str]],
    ) -> None:
        self.mode = mode
        self.missing_declaration = missing_declaration
        self.unresolved_layout = unresolved_layout
        parts: list[str] = []
        if missing_declaration:
            parts.append(
                f"liquid handlers without deck_layout: {missing_declaration}. "
                "Declare via LiquidHandler(name, ..., deck_layout='<name>')."
            )
        if unresolved_layout:
            pretty = [f"{name}.deck_layout={layout!r}" for name, layout in unresolved_layout]
            parts.append(
                f"declared layouts not in the per-device deck_layout_store: {pretty}. "
                "Seed via the stores factory at topology build time, or "
                "register at runtime via the deck-layout REST surface."
            )
        super().__init__(
            f"{mode.name} submissions require every liquid handler to declare a "
            f"deck_layout that resolves in its store. " + " ".join(parts)
        )


class OccupiedSlot(BaseModel):
    """One occupied start_location that blocks a submission.

    Carried in the `occupied` list of `StartLocationsOccupiedError` and
    serialized verbatim into the error envelope on the hosted wire so the
    operator sees exactly which location(s) to clear.

    ``source`` is narrowed to ``Literal["unknown"]``: the only call site
    cannot distinguish operator-placed vs prior-execution leftovers
    without provenance metadata on ``LabwareInstance``, and shipping the
    misleading wider hint is worse than shipping no hint. If/when
    provenance is added, widen the Literal back at the same time as the
    detection logic lands.
    """

    position_id: str
    existing_labware_id: str
    existing_labware_name: str
    existing_template_name: str
    source: Literal["unknown"] = "unknown"


class StartLocationsOccupiedError(RuntimeError):
    """Raised at submit time when an entry thread's start_location is occupied.

    The pre-check runs after `_resolve_acquisitions` and before any thread
    starts. Without this check, `ExecutingLabwareThread.initialize_labware`
    would retry `DeviceBusyError` from the occupied location forever -- the
    silent stall. Surfacing here turns it into a typed envelope the
    operator can act on (clear the location, then resubmit).

    Threads with a non-default spawn mode (e.g. reuse_existing) are skipped.
    Labware standing on its own thread's start, or named by this run and
    carried off by its own thread, is not counted as occupying the start.
    """

    def __init__(self, occupied: list[OccupiedSlot]) -> None:
        self.occupied = list(occupied)
        names = ", ".join(s.position_id for s in self.occupied)
        super().__init__(
            f"Cannot submit: {len(self.occupied)} start_location(s) occupied "
            f"by labware from a prior execution or operator placement: {names}. "
            f"Clear via `labware clear-submission`, `labware discharge`, or "
            f"`labware clear-all` before resubmitting."
        )


class ActiveExecutionRefusedError(RuntimeError):
    """Raised by the operator clear surface when a non-terminal execution
    blocks the clear and the caller did not pass `force=True`.

    Carries the offender (submission_id or labware_id) so the hosted wire
    layer can surface a typed CONFLICT envelope (matches the pattern
    set by `StartLocationsOccupiedError` and `SpawnIncompatibleError`).
    """

    def __init__(
        self,
        scope: Literal["submission", "labware", "all"],
        *,
        submission_id: str | None = None,
        labware_id: str | None = None,
    ) -> None:
        self.scope = scope
        self.submission_id = submission_id
        self.labware_id = labware_id
        if scope == "submission":
            assert submission_id is not None
            detail = f"submission '{submission_id}' is part of an active execution"
        elif scope == "labware":
            assert labware_id is not None
            detail = f"labware '{labware_id}' is referenced by an active thread"
        else:
            detail = "at least one active execution exists"
        super().__init__(
            f"Refusing clear: {detail}. Pass force=True to override."
        )


class UnknownLocationError(KeyError):
    """An operator named a location the system does not have."""

    def __init__(self, location: str) -> None:
        super().__init__(f"location {location!r} not found")
        self.location = location


class MethodNotFoundError(KeyError):
    """A workflow's bundle has no method by that name."""


class LabwareTemplateNotFoundError(KeyError):
    """The system declares no labware template by that name."""


class ExecutionNotFoundError(KeyError):
    """No execution has that id."""


class ThreadTemplateNotFoundError(KeyError):
    """A workflow declares no thread template by that name."""


class NotATransporterError(KeyError):
    """A teachpoint verb named a device that is not a transporter. Only a transporter has teachpoints."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"device {device_id!r} is not a transporter, and only a transporter has teachpoints")
        self.device_id = device_id


class LabwareInUseByThreadError(RuntimeError):
    """An operator tried to change the recorded position of labware a thread is using now."""


class LocationReservedError(RuntimeError):
    """An operator asked to put labware at a position another thread has
    reserved.

    A reservation is a thread's claim on somewhere to put a plate down, granted
    before the arm leaves, so a slot can be spoken for while it still looks
    empty. Writing labware onto one means the reserving move arrives to find
    the site taken and refuses, hours after the operator moved on.

    Two holders reach this refusal and they mean opposite things, so the
    message says which:

    - **A real plate is on its way here.** Nothing is wrong and nothing needs
      clearing. It lands, the operator takes it off, and the position is then
      free for what they were trying to put down. ``inbound_from`` says where
      it is now.
    - **Another placement was asked for here.** A person is already holding an
      instruction for this position. Doing that one is what frees it, or the
      claim can be cancelled.

    ``inbound_labware`` names the labware in both cases; ``awaiting_operator``
    is what says which of the two it is.

    A thread does NOT block the labware it is itself carrying: an operator
    finishing that thread's move by hand is exactly the case ``edit-location``
    exists for.
    """

    def __init__(
        self, position_id: str, reservation_id: str,
        holder_thread_id: str | None, holder_thread_name: str | None = None,
        inbound_labware: str | None = None, inbound_from: str | None = None,
        awaiting_operator: bool = False,
    ) -> None:
        self.position_id = position_id
        self.reservation_id = reservation_id
        self.holder_thread_id = holder_thread_id
        self.holder_thread_name = holder_thread_name
        self.inbound_labware = inbound_labware
        self.inbound_from = inbound_from
        self.awaiting_operator = awaiting_operator
        holder = (
            f"thread '{holder_thread_name or holder_thread_id}'"
            if holder_thread_id is not None else "no thread"
        )
        super().__init__(
            self._explain(position_id, reservation_id, holder)
        )

    def _explain(
        self, position_id: str, reservation_id: str, holder: str,
    ) -> str:
        if self.awaiting_operator:
            expected = (
                f" for {self.inbound_labware}" if self.inbound_labware else ""
            )
            return (
                f"{position_id} is already claimed{expected} by {holder}, "
                f"which is waiting for someone to place labware there. Doing "
                f"that placement frees the position; if it is not going to "
                f"happen, clear the claim first: `orca reservation cancel "
                f"{reservation_id} --execution <execution> --reason <reason>`, or abort the thread holding it."
            )
        if self.inbound_labware is not None:
            origin = f" from {self.inbound_from}" if self.inbound_from else ""
            # This refusal usually reaches someone who has already put the
            # plate down, and the engine cannot see labware nobody registered.
            # Telling them to wait without telling them to take theirs back
            # lands the arriving plate on top of it.
            return (
                f"{self.inbound_labware} is on its way to {position_id}"
                f"{origin}, carried by {holder}. Take your labware back off "
                f"{position_id} if you have already put it there, let that one "
                f"land, remove it, and then place yours. Cancelling the claim "
                f"({reservation_id}) sends that plate to a position you have "
                f"filled, so only do it if you are also stopping the thread."
            )
        # A plate already standing there cannot wait for the holder to finish:
        # it is what stops the holder finishing. Name a way out that works.
        return (
            f"{position_id} is reserved by {holder} ({reservation_id}). Name a "
            f"different position, or abort the thread holding it. `orca "
            f"reservation cancel {reservation_id} --execution <execution> "
            f"--reason <reason>` frees the spot only until "
            f"the holder asks for it again."
        )


class MoverHoldsNothingError(RuntimeError):
    """Raised when a mover-hold release names a mover the record says is empty.

    Its own type because the answer differs from every other miss: nothing is
    wrong, the jaws were already free, and the operator's next step is to look
    at what actually refused them. A state conflict rather than a missing key,
    so it sits with ``LocationReservedError`` and its message reaches the
    operator unquoted.
    """

    def __init__(self, mover_name: str) -> None:
        self.mover_name = mover_name
        super().__init__(
            f"The record has {mover_name!r} holding nothing, so there is no "
            f"hold to release."
        )


class LabwareNotFoundError(KeyError):
    """Raised when an operator clear / discharge targets a labware id that
    does not exist in any runtime store.

    Subclasses `KeyError` so older callers that catch `KeyError` keep
    working; the typed subclass lets the hosted wire layer pick a
    LABWARE_NOT_FOUND envelope code without isinstance-on-KeyError.
    """

    def __init__(self, labware_id: str) -> None:
        self.labware_id = labware_id
        super().__init__(f"No labware with id '{labware_id}'")


class BarcodeInUseError(RuntimeError):
    """Another labware already holds this barcode."""

    def __init__(self, barcode: str, existing_labware_id: str) -> None:
        self.barcode = barcode
        self.existing_labware_id = existing_labware_id
        super().__init__(
            f"Barcode {barcode!r} is already held by labware {existing_labware_id!r}"
        )


class NoLabwareCanOwnTheMethodError(ValueError):
    """A method run on its own names only labware its workflow reuses, and a reused labware cannot own the method."""

    def __init__(self, method_name: str, labware: list[str]) -> None:
        super().__init__(
            f"method {method_name!r} cannot run on its own over {labware}: each is reused where it stands, and one "
            f"labware that is not reused must own the method"
        )
        self.labware = labware


class EventHooksNotInTheWorkflowError(ValueError):
    """A standalone method run named events its workflow has no ``wf.on`` handler for."""

    def __init__(self, workflow_name: str, unknown: list[str], declared: list[str]) -> None:
        super().__init__(
            f"workflow {workflow_name!r} has no wf.on handler for {unknown}; its handlers are for {declared}"
        )
        self.unknown = unknown
        self.declared = declared
