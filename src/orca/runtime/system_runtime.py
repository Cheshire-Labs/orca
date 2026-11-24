"""Persistent runtime wrapping an orca-core System.

Accepts workflow submissions, tracks executions, and bridges
events to external systems via IEventSink and SystemEventBus.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping, TypeVar

from cheshire_drivers.gateway_protocol import DeviceConnectInfo, DeviceStatusInfo

from orca.runtime.labware_catalog_service import ILabwareCatalogService
from orca.devices.devices import LiquidHandler
from orca.events.event_bus_interface import IEventBus
from orca.gateway.controller.command_pipeline import NoAnswer
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.resource_models.command_effects import CommandStatement, stated_after_a_failure, stored_command
from orca.resource_models.labware_placement import Arrival
from orca.operations.error_mapping import error_label
from orca.gateway.controller.exceptions import DeviceError, DeviceLockedError
from orca.gateway.device_fault import DeviceFault
from orca.modes.device_modes import DeviceMode
from orca.runtime.facades.device_modes import (
    IDeviceModeFacade,
    ModeSwitch,
    ModeSwitchDuringRunError,
    RunInProgress,
    bring_up_error,
    mode_switch_warning,
)
from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.operator_commands import resolve_effective_interfaces
from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import (
    DeviceFaultContext,
    ExecutionContext,
    ExecutionLifecycleContext,
    ExecutionSimSpeedupContext,
    IncidentContext,
    SubmissionExecutionContext,
    ThreadExecutionContext,
    WorkflowExecutionContext,
)
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin
from orca.runtime.event_forwarder import _SystemEventForwarder
from orca.runtime.execution import Execution, ExecutionPhase, StopOutcome
from orca.modes.sim_pacing import SimPacer, validate_optional_sim_speedup, validate_sim_speedup
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.facades.devices import DeviceFacade
from orca.runtime.facades.incidents import IncidentFacade
from orca.runtime.facades.labware import LabwareFacade
from orca.runtime.facades.registry import RegistryFacade
from orca.runtime.facades.submissions import SubmissionFacade
from orca.runtime.facades.threads import ThreadFacade
from orca.runtime.execution_record_service import IExecutionRecordService
from orca.runtime.event_saves import END_EVENTS_SAVE_TIMEOUT_S, IEventSaves
from orca.runtime.unsaved_record import IUnsavedRecordReporter, UnsavedRecord
from orca.runtime.incident_service import IIncidentService
from orca.runtime.incident_store import (
    DeckReconcileConflictDetail,
    IncidentCategory,
    IncidentSeverity,
    RecoverableTimeoutContext,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.incident_declarer import IncidentDeclarer
from orca.runtime.recoverable_timeout import RecoverableTimeoutCoordinator
from orca.system.reservation_manager.location_reservation import ReservationPriority
from orca.runtime.access_config_service import IAccessConfigService
from orca.runtime.move_defaults_service import IMoveDefaultsService
from orca.runtime.grip_profile_service import IGripProfileService
from orca.resource_models.store_interfaces import (
    IAccessConfigStore,
    IDeploymentProfileStore,
)
from orca.events.emitter import IEventSink
from orca.resource_models.labware_service_interface import ILabwareService
from orca.resource_models.labware import LabwareInstance, TipRackInstance
from orca.resource_models.location import Location
from orca.workflow_models.labware_group import (
    AcquisitionValidationError,
    BarcodeAcquisition,
    LabwareGroup,
    LabwareGroupMember,
    LocationAcquisition,
)
from orca.state.projections import (
    contents_provenance,
    has_tip_baseline,
    has_volume_history,
    unsettled_gaps,
)
from orca.state.mounted import head_a_failed_command_leaves_unknown
from orca.state.provenance import Provenance
from orca.state.unsettled import (
    SettleNoun,
    UnsettledSubject,
    is_unsettled,
    set_verb,
    settle_verb,
)
from orca.state.records import ObservationGapCause, OperationRecord, TrackingRecord
from orca.state.ops_store import SYSTEM_ID
from orca.modes.run_modes import (
    OUT_OF_RUN_BASE,
    WorkflowRunMode,
    current_run_mode,
    mode_scope,
)
from orca.modes.sim_diagnostics import (
    maybe_enable_sim_coroutine_diagnostics_from_env,
)
from orca.runtime.errors import (
    EventHooksNotInTheWorkflowError,
    NoLabwareCanOwnTheMethodError,
    DeckLayoutRequiredError,
    OtherRunModeActiveError,
    RunModeMismatchError,
    OccupiedSlot,
    RunModeRequiredError,
    StartLocationsOccupiedError,
    SubmissionBlockedByOrphanedBacklogError,
    SimSpeedupNotApplicableError,
    SubmissionToPausedExecutionError,
    DeploymentProfileNotFoundError,
    ExecutionNotFoundError,
    LabwareTemplateNotFoundError,
    LabwareNotFoundError,
    MethodNotFoundError,
    ThreadTemplateNotFoundError,
    UnknownLocationError,
)
from orca.runtime.facade_interfaces import (
    IDeckLayoutFacade,
    IDeviceFacade,
    IIncidentFacade,
    ILabwareFacade,
    IOpsHistoryFacade,
    IRegistryFacade,
    ISubmissionFacade,
    ITeachpointFacade,
    IThreadFacade,
    IVariableFacade,
)
from orca.runtime.registry_protocols import (
    IDeviceConnectionSource,
    IDeviceFaultSource,
    IDeviceRegistry,
    IGatewayRegistry,
    ITopologyRegistry,
)
from orca.runtime.runtime_state import RuntimeState
from orca.runtime.stall_detector import StallDetector, StallReport, SystemStallError, ThreadStallSnapshot, snapshot_in_flight
from orca.runtime.blockers import Blocker, LabwareOccupant, derive_blockers
from orca.runtime.stored_labware import adopt_stored_labware
from orca.runtime.status_models import (
    DeviceSnapshot,
    ExecutionDetail,
    ExecutionStatus,
    PendingManualStepRecord,
    PluginSnapshot,
    ReservationSnapshot,
    ThreadSnapshot,
)
from orca.runtime.status_builders import _build_thread_snapshot, derive_waiting_for
from orca.workflow_models.submission import BatchMode, ResolvedAcquisition, Submission, SubmissionStatus
from orca.runtime.system_event_bus import SystemEventBus
from orca.system.errors import DeviceInitializationError, DevicesNotReadyError
from orca.system.system_interface import (
    DeckConflictReason,
    DeckReconcileConflict,
    NOTHING_ON_RECORD_WAS_MOVED,
    ISystem,
)
from orca.variables.deployment_profile import DeploymentProfile
from orca.variables.errors import OptionValue, SubmissionInputsRefusedError
from orca.variables.submission_inputs import SubmittedInputs, refusals_for
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.standalone_method_workflow import (
    StandaloneThreadSpec,
    build_standalone_method_workflow,
    starts_from_reuse,
    with_the_workflows_kinds,
)
from orca.workflow_models.status_enums import LabwareThreadStatus, RecoveryDecision, WorkflowStatus
from orca.workflow_models.thread_template import ThreadTemplate, thread_named, threads_a_group_names
from orca.workflow_models.acquisition import MemberFacts, no_stated_tips, refusal_for
from orca.workflow_models.workflow_templates import EventHookInfo, WorkflowTemplate
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow

logger = logging.getLogger(__name__)

_T = TypeVar("_T", bound=OrcaPlugin)

# Long enough for a thread to unwind a move, short enough that a wedged one
# cannot hold a process open.
_SHUTDOWN_DRAIN_TIMEOUT_S = 30.0
# A cancel is one websocket write, and a rebuild's budget is the whole
# shutdown, so a wedged one must not spend it.
_CANCEL_DRAIN_TIMEOUT_S = 5.0


async def _bounded(
    awaitable: Awaitable[object], timeout: float, what: str,
) -> None:
    """Await something during shutdown, and give up out loud."""
    try:
        await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "Shutdown gave up waiting %.0fs for %s; continuing.", timeout, what,
        )


# How the operator settles it is the same everywhere, so it is said once.
_SETTLE_IT = (
    "Put it where it really is with edit-labware-location, or discharge it "
    "after physically removing it."
)


def _deck_conflict_message(conflict: DeckReconcileConflict) -> str:
    """Say what the two sides claim, in the words the operator's verbs use."""
    labware = conflict.labware_name
    device = conflict.device_name
    where = conflict.position_id
    driver_site = conflict.driver_site
    if conflict.reason is DeckConflictReason.SITE_NOT_IN_LAYOUT:
        return (
            f"Labware '{labware}' sits at '{where}' but the active deck layout "
            f"of '{device}' no longer provides that site, so it was not "
            f"projected to the driver. {_SETTLE_IT}"
        )
    if conflict.reason is DeckConflictReason.HELD_BY_GRIPPER:
        return (
            f"The ledger has '{labware}' in '{device}'s gripper. A deck "
            f"addresses labware by site and the jaws are not one, so the two "
            f"models cannot both be right about where it is. {_SETTLE_IT}"
        )
    if conflict.reason is DeckConflictReason.DRIVER_SITE_DIFFERS:
        return (
            f"'{device}' has '{labware}' at '{driver_site}' and the ledger has "
            f"it at '{where}'. Something moved it outside orca. {_SETTLE_IT}"
        )
    if conflict.reason is DeckConflictReason.MISSING_FROM_DRIVER:
        return (
            f"The ledger puts '{labware}' at '{where}' and '{device}' has it "
            f"nowhere on its deck. Either it left the deck, or the driver's "
            f"session was rebuilt and lost it. {_SETTLE_IT}"
        )
    if conflict.reason is DeckConflictReason.UNKNOWN_TO_LEDGER:
        return (
            f"'{device}' has labware '{labware}' at '{driver_site}' that the "
            f"ledger does not place on this deck. Register it with "
            f"register-labware if it is really there, or remove it at the "
            f"instrument."
        )
    if conflict.reason is DeckConflictReason.LEDGER_TARGET_OCCUPIED:
        return (
            f"A command on '{device}' put '{labware}' at '{where}', "
            f"where the ledger already has '{conflict.blocking_labware_name}'. "
            f"The command ran, but the record was left unchanged: {conflict.detail} "
            f"Settle where '{conflict.blocking_labware_name}' is first. {_SETTLE_IT}"
        )
    if conflict.reason is DeckConflictReason.CONTENTS_DIFFER:
        return (
            f"'{device}' and the record disagree about what '{labware}' at "
            f"'{where}' holds. {conflict.detail or ''} The driver holds what "
            f"orca projected onto it, so the record is the one to believe "
            f"unless someone looked. Confirm or correct it with "
            f"set-tip-state or set-well-volumes."
        ).replace("  ", " ")
    if labware is None:
        return (
            f"A gripper move on '{device}' from '{where}' to '{driver_site}' did "
            f"not finish. {NOTHING_ON_RECORD_WAS_MOVED}"
        )
    return (
        f"A gripper move of '{labware}' on '{device}' did not finish. The "
        f"driver still names '{where}', which is where the move BEGAN, and it "
        f"was going to '{driver_site}'. Look before commanding motion, then "
        f"{_SETTLE_IT[0].lower()}{_SETTLE_IT[1:]}"
    )


# `ExecutionStatus` lives in `status_models.py` so the runtime
# protocol (`runtime_interface.py`) can refer to it without importing
# from `system_runtime` (which would induce a cycle).


_TERMINAL_PHASES = frozenset({
    ExecutionPhase.COMPLETED, ExecutionPhase.PARTIAL_COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.ABORTED,
})


_UNPACED_REASON: dict[WorkflowRunMode, str] = {
    WorkflowRunMode.LIVE: "a LIVE run is paced by its hardware",
    WorkflowRunMode.CHECK_RUN: "a check run is never paced",
}


def _check_sim_speedup_applies(mode: WorkflowRunMode, sim_speedup: float | None) -> None:
    """A speed-up is a number in SIM_SPEEDUP_RANGE, and only a sim run can carry one."""
    if sim_speedup is None:
        return
    validate_sim_speedup(sim_speedup)
    if mode is not WorkflowRunMode.SIM:
        raise SimSpeedupNotApplicableError(_UNPACED_REASON[mode])


def _refuse_a_second_profile(existing: Execution, deployment_profile: str | None) -> None:
    """An execution loads one profile, when it starts."""
    started_with = existing.submissions[0].deployment_profile if existing.submissions else None
    if deployment_profile is not None and deployment_profile != started_with:
        raise ValueError(
            f"execution {existing.id} started with deployment profile {started_with!r}, so a "
            f"JOIN_EXISTING submission cannot load {deployment_profile!r}. Submit STANDALONE "
            "to start a new execution.",
        )


_END_STATUS: dict[ExecutionPhase, str] = {
    ExecutionPhase.COMPLETED: "COMPLETED",
    ExecutionPhase.PARTIAL_COMPLETED: "PARTIAL_COMPLETED",
    ExecutionPhase.FAILED: "FAILED",
    ExecutionPhase.ABORTED: "ABORTED",
}
_END_SUBMISSION_STATUS: dict[ExecutionPhase, SubmissionStatus] = {
    ExecutionPhase.COMPLETED: SubmissionStatus.COMPLETED,
    ExecutionPhase.PARTIAL_COMPLETED: SubmissionStatus.PARTIAL_COMPLETED,
    ExecutionPhase.FAILED: SubmissionStatus.FAILED,
    ExecutionPhase.ABORTED: SubmissionStatus.ABORTED,
}


def run_phase_of(statuses: Sequence[LabwareThreadStatus]) -> ExecutionPhase:
    """The phase a run ends in, from its threads' final statuses, in the order the rules apply."""
    if LabwareThreadStatus.FAILED in statuses:
        return ExecutionPhase.FAILED
    if LabwareThreadStatus.STOPPED in statuses:
        return ExecutionPhase.ABORTED
    if LabwareThreadStatus.ABORTED in statuses:
        if LabwareThreadStatus.COMPLETED in statuses:
            return ExecutionPhase.PARTIAL_COMPLETED
        return ExecutionPhase.ABORTED
    return ExecutionPhase.COMPLETED


def _phase_to_state(phase: ExecutionPhase) -> ExecutionState:
    """Map ExecutionPhase to the flat ExecutionState for the daemon API.

    ACCEPTING + DRAINING + STOPPING all surface as RUNNING -- the flat
    shim represents the coarse "is this active?" distinction; STOPPING
    is a finer phase visible on the richer ExecutionStatus / DTO surface.
    Terminal states share string values across enums, so the value-construction path works directly.
    """
    if phase in (
        ExecutionPhase.ACCEPTING, ExecutionPhase.DRAINING, ExecutionPhase.STOPPING,
    ):
        return ExecutionState.RUNNING
    return ExecutionState(phase.value)


def _stall_wait_subject(thread: ExecutingLabwareThread) -> str | None:
    """What a thread is waiting on, for the stall report.

    ``derive_waiting_for`` answers by status, and a parked contributor's status
    is ``EXECUTING_ACTION``, so it would report nothing at all. Name the action
    it is following instead, or the report lists a wedged thread with no subject.
    """
    subject = derive_waiting_for(thread)
    if subject is not None or not thread.following_peer_action:
        return subject
    action = thread.assigned_action
    return action.action.command if action is not None else None


def _fault_named_by_pause(thread: ExecutingLabwareThread) -> DeviceFault | None:
    """The fault this thread's error pause is about, or None.

    The error carries it. A command that faulted a device is handed the fault it
    left, and a command refused by a fault already standing carries that one, so
    a failed move and a failed device call answer the same way.

    Only while the thread is error-paused. An aborted thread keeps its last
    error on purpose, and that error must not be read later as an operator
    saying the device was looked at.
    """
    if not thread.is_error_paused:
        return None
    error = thread.last_error
    return error.device_fault if isinstance(error, DeviceError) else None


def _contents_detail(
    provenance: Provenance, name: str, *, settle_with: str,
    gaps: frozenset[ObservationGapCause] = frozenset(),
) -> str:
    if provenance is Provenance.UNKNOWN:
        return f"nothing has ever said what {name!r} holds, which is not the same as empty"
    if ObservationGapCause.OPERATIONS_DROPPED in gaps:
        return _dropped_detail(f"what {name!r} holds", settle_with)
    return f"{name!r} was known, then a stretch passed with nobody watching it"


def _dropped_detail(subject: str, settle_with: str) -> str:
    """A stop, a timeout or a failure cut a command off at the device, so nobody knows what it did.

    Only stating what is really there settles it, which is why the confirm
    verbs refuse here.
    """
    return (
        f"a stop, a timeout or a failure cut a command off at the device, so {subject} "
        f"may be wrong by an amount nothing can state; look and state what is "
        f"there with {settle_with} rather than confirming what the record shows"
    )


def _contents_noun(labware: LabwareInstance) -> SettleNoun:
    """Naming a verb that refuses the labware is worse than naming none:
    `set-well-volumes` refuses a rack and `set-tip-state` refuses a plate."""
    return "tip-state" if isinstance(labware, TipRackInstance) else "well-volumes"


def _confirm_has_something_to_back_it(
    labware: LabwareInstance, ops: list[OperationRecord],
) -> bool:
    """Whether the confirm verb for this labware would find anything to agree
    with, asked with the same projection the facade refuses on.

    A baseline is not enough. An undeclared trough is seeded with an all-zero
    INITIAL_STATE, which counts as a contents baseline, so its provenance is
    not UNKNOWN -- but its volumes fold to nothing and `confirm_well_volumes`
    refuses. Reading provenance alone would name that row a verb that errors.
    """
    if isinstance(labware, TipRackInstance):
        return has_tip_baseline(ops, labware.name)
    return has_volume_history(ops, labware.name)


def _head_detail(
    provenance: Provenance, name: str, *,
    gaps: frozenset[ObservationGapCause] = frozenset(),
) -> str:
    if provenance is Provenance.UNKNOWN:
        return f"nothing has ever said what {name!r} is carrying"
    if ObservationGapCause.OPERATIONS_DROPPED in gaps:
        return _dropped_detail(
            f"what {name!r} is carrying", set_verb("mounted-tips"),
        )
    return f"what {name!r} is carrying was known, then nobody was watching"


def _what_to_state(command: str) -> str:
    if head_a_failed_command_leaves_unknown(command) is None:
        return "the labware it used"
    return "the labware it used and its head"


def _outcome_unknown_message(device_name: str, command: str, seconds: float, why: NoAnswer) -> str:
    if why is NoAnswer.FAILED:
        return (
            f"'{command}' on '{device_name}' failed after the device took it, so what it did is unknown. Look at "
            f"the device and state what {_what_to_state(command)} now hold. Then mark the command complete "
            f"if it did finish, or abort it, to settle the record."
        )
    cause = (
        f"got no answer within its time limit ({seconds:.1f}s)" if why is NoAnswer.TIMED_OUT
        else "was stopped while the device was running it"
    )
    return (
        f"'{command}' on '{device_name}' {cause}, so what it did is unknown. Look at the device and confirm "
        f"where the plate is, then mark the command complete or abort it to settle the record."
    )


def _moves_off(
    workflow: WorkflowTemplate,
    claims: Sequence[tuple[tuple[str, str], ResolvedAcquisition]],
    labware_id: str,
    start: Location,
    past_their_starts: frozenset[str],
) -> bool:
    """Whether named labware standing on `start` will be carried off it.

    A thread that has not reached its start yet first carries its plate there, so that start must clear in turn.
    A chain that comes back to a start it already passed never clears: each plate waits for the next. A plate in
    `past_their_starts` belongs to a thread already working, which carries it on to its next step.
    """
    destinations: dict[str, tuple[ThreadTemplate, Location]] = {}
    for (_, thread_name), claim in claims:
        template = _entry_thread_named(workflow, thread_name)
        if claim.labware_instance is not None and template is not None:
            destinations[claim.labware_instance.id] = (template, claim.start_location or template.start_location)
    passed: set[str] = set()
    here = start
    while True:
        entry = destinations.get(labware_id)
        if entry is None:
            return False
        template, own_start = entry
        if template.end_leave_in_place and any(end.position_id == here.position_id for end in template.end_locations):
            return False
        if labware_id in past_their_starts or own_start.position_id == here.position_id or own_start.is_plate_source:
            return True
        passed.add(here.position_id)
        if own_start.position_id in passed:
            return False
        occupant = own_start.labware
        if occupant is None:
            return True
        labware_id, here = occupant.id, own_start


_ADOPTED_GROUP_ID = "operator-placed"


def _occupied_slot(loc: Location) -> OccupiedSlot | None:
    """The slot record for a start location that is already holding labware.

    Plate sources hold many plates by design, so a non-empty output slot means
    "next plate ready", not "stage blocked", and refusing on it would reject a
    legitimate resubmit against the same source.
    """
    if loc.is_plate_source:
        return None
    labware = loc.labware
    if labware is None:
        return None
    return OccupiedSlot(
        position_id=loc.position_id,
        existing_labware_id=labware.id,
        existing_labware_name=labware.name,
        existing_template_name=labware.template_name,
    )


def _entry_thread_named(workflow: WorkflowTemplate, name: str) -> ThreadTemplate | None:
    return thread_named(workflow.entry_thread_templates, name)


def _the_hooks_to_carry(workflow: WorkflowTemplate, chosen: Sequence[str] | None) -> list[EventHookInfo]:
    """The workflow's ``wf.on`` handlers a standalone run of one of its methods fires."""
    if chosen is None:
        return list(workflow.event_hooks)
    declared = sorted({hook.event_name for hook in workflow.event_hooks})
    unknown = sorted(set(chosen) - set(declared))
    if unknown:
        raise EventHooksNotInTheWorkflowError(workflow.name, unknown, declared)
    return [hook for hook in workflow.event_hooks if hook.event_name in chosen]


class SystemRuntime:
    """Long-lived runtime wrapping a built System.

    Lifecycle: CREATED -> start() -> RUNNING -> stop() -> STOPPED.
    Workflows can only be submitted while RUNNING.
    """

    def __init__(
        self,
        system: ISystem,
        *,
        incident_service: IIncidentService,
        execution_record_service: IExecutionRecordService,
        unsaved_records: IUnsavedRecordReporter,
        event_saves: IEventSaves,
        access_config_service: IAccessConfigService,
        move_defaults_service: IMoveDefaultsService,
        grip_profile_service: IGripProfileService,
        profile_store: IDeploymentProfileStore,
        labware_catalog_store: ILabwareCatalogService,
        gateway_registry: IGatewayRegistry,
        connection_source: IDeviceConnectionSource,
        device_gateway: DeviceGateway,
        event_bus: IEventBus,
        system_event_bus: SystemEventBus,
        topology_registry: ITopologyRegistry,
        device_registry: IDeviceRegistry,
        variables: IVariableFacade,
        teachpoints: ITeachpointFacade,
        deck_layouts: IDeckLayoutFacade,
        ops_history: IOpsHistoryFacade,
        device_modes: IDeviceModeFacade,
        stall_check_interval: float | None,
        stall_required_stable_ticks: int,
        default_sim_speedup: float | None,
    ) -> None:
        self._system = system
        # What a simulated submission runs at when it names no speed-up of its own; None does not wait.
        self._default_sim_speedup = validate_optional_sim_speedup(default_sim_speedup)
        self._labware_service: ILabwareService = system.labware_service
        # Activity services are opened and closed by whoever built them.
        self._incident_service = incident_service
        self._execution_record_service = execution_record_service
        self._unsaved_records = unsaved_records
        self._event_saves = event_saves
        unsaved_records.add_run_pauser(self._pause_for_an_unsaved_record)
        # Owns the recoverable-timeout race + held-dispatch registry. Seeded
        # onto the per-thread ContextVar at thread start so device dispatch can
        # park a timed-out call here for an operator decision.
        self._recoverable_timeouts = RecoverableTimeoutCoordinator(self)
        # Late-bind the IncidentDeclarer on the executing-thread factory. Every
        # ExecutingLabwareThread spawned after this returns receives the back-ref
        # so it can record incidents. Must run AFTER _incident_service and
        # _recoverable_timeouts are bound.
        self._incident_declarer = IncidentDeclarer(
            self._incident_service, self.pause_execution, self._recoverable_timeouts, self.sim_pacer_for,
        )
        self._system.set_thread_incident_declarer(self._incident_declarer)
        self._system.set_thread_status_recorder(self)
        self._labware_catalog_store = labware_catalog_store
        self._access_config_service = access_config_service
        self._move_defaults_service = move_defaults_service
        self._grip_profile_service = grip_profile_service
        self._profile_store = profile_store
        self._topology_registry = topology_registry
        self._gateway_registry = gateway_registry
        self._connection_source = connection_source
        self._device_controller = device_gateway.controller
        self._connection_events = device_gateway.events
        # Unified two-card view (topology + connection sources). The
        # runtime.topology / runtime.gateway views coexist with it because
        # their consumers read different entry types; unifying them onto this
        # surface is a separate refactor.
        self._device_registry = device_registry
        self._location_listener_registered = False
        self._deck_reseed_tasks: set[asyncio.Task[None]] = set()
        self._connection_listener_registered = False
        self._state = RuntimeState.CREATED
        self._executions: dict[str, Execution] = {}
        # Every plate a run has bound, kept after its run is removed; a restart forgets them.
        self._plates_used_by_runs: set[str] = set()
        # Index of live, group-bearing executions keyed by workflow.name.
        # Populated on first grouped submit; cleared in _on_task_done.
        # Enables mid-run submission injection: a second submit() with groups
        # for the same workflow reuses the live Execution instead of booting
        # a fresh one. Groupless submits (legacy one-shot) bypass this index
        # and always get independent executions.
        self._active_executions: dict[str, Execution] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._stall_detectors: dict[str, StallDetector] = {}
        self._stall_check_interval = stall_check_interval
        self._stall_required_stable_ticks = stall_required_stable_ticks
        self._stall_task: asyncio.Task[None] | None = None
        self._plugins: list[OrcaPlugin] = []
        self._plugin_listeners: dict[int, Callable[[RuntimeEvent], None]] = {}
        self._executing_workflows: list[ExecutingWorkflow] = []

        self._system_event_bus = system_event_bus
        self._system.ops_history.announce_saves_to(self._announce_saved)
        self._forwarder = _SystemEventForwarder(self._system_event_bus)
        self._event_bus = event_bus
        event_bus.subscribe_all(self._forwarder)
        # Broadcast every recorded incident as an INCIDENT RuntimeEvent so
        # operator notifiers see faults without polling incidents_list.
        self._incident_service.on_record = self._emit_incident_event

        # Sub-facades. Instantiated once; property accessors return the same
        # instance across calls so a hosted deployment (later) can layer per-facade
        # org-scoping. Some facades close over self; be careful with ordering.
        self._variables = variables
        self._labware = LabwareFacade(system, self._labware_service, self)
        self._devices = DeviceFacade(system, self._connection_source, self, device_gateway)
        self._threads = ThreadFacade(self, system)
        # Evict per-thread mutation locks once the thread reaches a
        # terminal state. No archive list -- locks have no useful
        # post-completion state.
        self._system_event_bus.subscribe(self._threads.on_thread_terminal_event)
        self._registry = RegistryFacade(
            system,
            list_reservations_fn=self.list_reservations,
            cancel_reservation_fn=self.cancel_reservation,
            connections=self._connection_source,
            faults=self._device_controller,
            base_mode=self.operator_base_mode,
            on_workflow_added=self._restore_labware_its_workflow_declares,
        )
        self._incidents = IncidentFacade(
            self._incident_service, self._recoverable_timeouts, system.command_pipeline, self.clear_device_fault,
        )
        self._teachpoints = teachpoints
        self._deck_layouts = deck_layouts
        self._submissions = SubmissionFacade(self)
        self._ops_history = ops_history
        self._device_modes = device_modes
        self._device_mode_lock = asyncio.Lock()

    # -- Sub-facade properties (ISystemRuntime namespace) -------------------

    @property
    def variables(self) -> IVariableFacade:
        return self._variables

    @property
    def labware(self) -> ILabwareFacade:
        return self._labware

    @property
    def device_modes(self) -> IDeviceModeFacade:
        return self._device_modes

    @property
    def devices(self) -> IDeviceFacade:
        return self._devices

    async def unsettled_state(self) -> list[UnsettledSubject]:
        """Everything the record cannot answer, as one worklist.

        Scattered across surfaces these read as unrelated warnings; together
        they are what an operator walking up to a paused system has to settle.
        """
        unsettled: list[UnsettledSubject] = []
        for labware in self._system.labwares:
            # One read: `of` already folds the ops that say whether anything
            # has ever happened to this labware, and searching every execution
            # bucket twice for them is the expensive half of this call.
            ops = await self._system.labware_contents.ops_of(labware.ref)
            provenance = contents_provenance(ops, labware.name)
            if not is_unsettled(provenance):
                continue
            if provenance is Provenance.UNKNOWN and not ops:
                # An ordinary destination plate: its template declared nothing
                # to put in it and nothing has happened to it yet. Listing
                # every one of those is what makes the worklist unreadable.
                continue
            gaps = unsettled_gaps(ops, labware.name)
            noun = _contents_noun(labware)
            unsettled.append(UnsettledSubject(
                subject=labware.name,
                subject_id=labware.id,
                subject_kind="labware",
                provenance=provenance,
                detail=_contents_detail(
                    provenance, labware.name,
                    settle_with=set_verb(noun),
                    gaps=gaps,
                ),
                settle_with=settle_verb(
                    noun, provenance, gaps=gaps,
                    can_be_agreed_with=_confirm_has_something_to_back_it(
                        labware, ops,
                    ),
                ),
            ))
        for handler in self._system.handlers_with_a_head():
            mounted = await self._system.mounted_tips.of(handler)
            if not is_unsettled(mounted.provenance):
                continue
            unsettled.append(UnsettledSubject(
                subject=handler,
                subject_id=None,
                subject_kind="device_head",
                provenance=mounted.provenance,
                detail=_head_detail(
                    mounted.provenance, handler,
                    gaps=mounted.gaps,
                ),
                settle_with=settle_verb(
                    "mounted-tips", mounted.provenance,
                    gaps=mounted.gaps,
                    # A head's read is the channels it carries; there is no
                    # separate baseline that can be present but empty.
                    can_be_agreed_with=True,
                ),
            ))
        return unsettled

    @property
    def threads(self) -> IThreadFacade:
        return self._threads

    @property
    def registry(self) -> IRegistryFacade:
        return self._registry

    @property
    def incidents(self) -> IIncidentFacade:
        return self._incidents

    @property
    def execution_records(self) -> IExecutionRecordService:
        """The execution-record Service this runtime persists/reads executions through.

        Exposed so the daemon's get/list/detail execution operations fall back to
        the persisted terminal record when the live runtime no longer holds it.
        """
        return self._execution_record_service

    @property
    def labware_catalog_store(self) -> ILabwareCatalogService:
        """The catalog Service (lock + policy) this runtime reads/writes through.

        Exposed so the deployment layer shares the same Service instance.
        """
        return self._labware_catalog_store

    @property
    def access_config_store(self) -> IAccessConfigStore:
        """The access-config registry this runtime resolves teachpoints against.

        Exposed (not the facade) so the deployment-registries layer shares the
        SAME service instance. Operator CRUD lives on that layer, never on the
        runtime; the runtime only reads this store at resolution time. The
        service satisfies ``IAccessConfigStore``, so consumers treat it as a store.
        """
        return self._access_config_service

    @property
    def move_defaults_service(self) -> IMoveDefaultsService:
        """The per-transporter move-parameter defaults this runtime resolves against.

        Exposed (not a facade) for the same single-source reason as
        ``access_config_store``: operator CRUD lives on the deployment layer and
        the runtime only reads this at resolution time.
        """
        return self._move_defaults_service

    @property
    def grip_profile_service(self) -> IGripProfileService:
        """How each labware type is held, layered over a transporter's defaults.

        Exposed for the same single-source reason as ``move_defaults_service``:
        operator CRUD lives on the deployment layer and the runtime only reads
        this at resolution time.
        """
        return self._grip_profile_service

    @property
    def profile_store(self) -> IDeploymentProfileStore:
        """The deployment-profile registry this runtime resolves submits against.

        Exposed (not the facade) for the same single-source reason as
        ``access_config_store``: operator CRUD is on the deployment layer.
        """
        return self._profile_store

    @property
    def teachpoints(self) -> ITeachpointFacade:
        return self._teachpoints

    @property
    def deck_layouts(self) -> IDeckLayoutFacade:
        return self._deck_layouts

    @property
    def submissions(self) -> ISubmissionFacade:
        return self._submissions

    @property
    def topology(self) -> ITopologyRegistry:
        return self._topology_registry

    @property
    def gateway(self) -> IGatewayRegistry:
        return self._gateway_registry


    @property
    def device_faults(self) -> IDeviceFaultSource:
        return self._device_controller

    @property
    def device_registry(self) -> IDeviceRegistry:
        """Unified two-card device registry.

        Composes `runtime.topology` (declared) and `runtime.gateway`'s
        connection-source counterpart (reachable now) into a single read
        surface keyed by device name. Live state (`is_connected`,
        `is_initialized`) is queried per call against the connection source
        and the in-process driver. `runtime.topology` and `runtime.gateway`
        coexist with this view because their consumers read
        TopologyDeviceEntry / GatewayDeviceEntry directly; unifying onto this
        surface is a separate refactor.
        """
        return self._device_registry

    @property
    def ops_history(self) -> IOpsHistoryFacade:
        return self._ops_history

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def system(self) -> ISystem:
        return self._system

    def register_plugin(self, plugin: OrcaPlugin, *, subscribe: bool = True) -> None:
        """Register a plugin and inject the system. It hears this runtime's events unless `subscribe` is False.

        A server's configured plugins pass False: they hear every event on the server's event hub instead.
        """
        self._plugins.append(plugin)
        plugin.set_system(self._system)
        if not subscribe:
            return
        listener = plugin.handle_runtime_event
        self._plugin_listeners[id(plugin)] = listener
        self._system_event_bus.subscribe(listener)

    def register_sink(self, sink: IEventSink) -> None:
        """Register an event sink to receive all RuntimeEvents."""
        self._system_event_bus.subscribe(sink.on_event)

    def unregister_sink(self, sink: IEventSink) -> None:
        """Stop delivering RuntimeEvents to a previously registered sink.

        Symmetric with register_sink so a transient consumer (a disconnecting
        UI/AI client, a scoped test waiter) can detach instead of leaking a
        listener on the bus for the runtime's lifetime.
        """
        self._system_event_bus.unsubscribe(sink.on_event)

    def _emit_incident_event(self, incident: SystemIncident) -> None:
        """Broadcast an INCIDENT RuntimeEvent for a recorded incident.

        Summary fields only; the full typed detail stays queryable on the
        incidents surface (incidents_get / GET /api/incidents) keyed by
        incident_id.
        """
        context = IncidentContext(
            incident_id=incident.id,
            category=incident.category.value,
            severity=incident.severity.value,
            message=incident.message,
            recovery_action=incident.recovery_action.value,
            execution_id=incident.execution_id,
            thread_id=incident.thread_id,
        )
        event = RuntimeEvent(
            event_name=f"INCIDENT.{incident.id}.{incident.category.value}",
            execution_id=incident.execution_id or SYSTEM_ID,
            timestamp=incident.timestamp,
            entity_type="INCIDENT",
            entity_id=incident.id,
            status=incident.category.value,
            context=context,
        )
        self._system_event_bus.emit(event)

    def _on_device_fault_changed(
        self, device_name: str, fault: DeviceFault | None,
    ) -> None:
        """Put a latched or cleared device fault on the event bus.

        Without this the fault is state on the device row and nothing else, so
        no observer sees it arrive and an operator finds out when the next
        thread refuses. A cleared fault is emitted too, so a surface that
        latched onto the fault can drop it without polling.
        """
        cleared = fault is None
        context = DeviceFaultContext(
            device_name=device_name,
            cleared=cleared,
            command=None if fault is None else fault.command,
            outcome=None if fault is None else fault.outcome.value,
            error=None if fault is None else fault.error,
            error_type=None if fault is None else fault.error_type,
            may_still_be_moving=False if fault is None else fault.may_still_be_moving,
            message=None if fault is None else fault.describe(),
            execution_id=None if fault is None else fault.execution_id,
        )
        status = "FAULT_CLEARED" if cleared else "FAULTED"
        self._system_event_bus.emit(RuntimeEvent(
            event_name=f"DEVICE.{device_name}.{status}",
            execution_id=(
                SYSTEM_ID if fault is None or fault.execution_id is None
                else fault.execution_id
            ),
            timestamp=time.time(),
            entity_type="DEVICE",
            entity_id=device_name,
            status=status,
            context=context,
        ))

    def _announce_saved(self, record: TrackingRecord) -> None:
        """Tell displays the record now holds this, once per device it names."""
        devices = sorted({op.device_name for op in record.operations if op.device_name})
        for entity_id in devices or [SYSTEM_ID]:
            self._system_event_bus.emit(RuntimeEvent(
                event_name=f"CONTENTS.{entity_id}.SAVED",
                execution_id=record.execution_id,
                timestamp=time.time(),
                entity_type="CONTENTS",
                entity_id=entity_id,
                status="SAVED",
                context=WorkflowExecutionContext(
                    execution_id=record.execution_id, workflow_name=SYSTEM_ID,
                ),
            ))

    def record_thread_status(
        self, execution_id: str, *, thread_id: str, name: str,
        template_name: str, status: str,
        last_error: str | None, pause_reason: str | None, group_id: str | None,
    ) -> None:
        """Save a thread's status change in the run's record, for runs this runtime accepted."""
        if execution_id not in self._executions:
            return
        self._execution_record_service.record_thread(
            execution_id, thread_id=thread_id, name=name,
            template_name=template_name, status=status,
            last_error=last_error, pause_reason=pause_reason, group_id=group_id,
        )

    @property
    def plugins(self) -> list[OrcaPlugin]:
        return list(self._plugins)

    def get_plugin(self, plugin_type: type[_T]) -> _T:
        """Get a registered plugin by type. Raises KeyError if not found."""
        for p in self._plugins:
            if isinstance(p, plugin_type):
                return p
        raise KeyError(f"No plugin of type {plugin_type.__name__} registered")

    async def start(self, *, after_reload: bool = False) -> None:
        """Mark the runtime RUNNING; defer device dispatch to first-thread-touch.

        ``after_reload``: the previous runtime in this process shut down just now, so the
        record was watched throughout and boot writes no restart gap. A process start leaves it False.

        Boot does not walk LiquidHandler decks or initialize devices. The
        execution entrypoint, `_run_workflow`, calls
        `System.ensure_runtime_initialized` at its top, handing it the
        workflow so the walk covers what that workflow declares rather
        than every device in the topology. The first call for a given
        (mode, scope) triggers the configure + initialize walk for that
        mode's world, idempotent thereafter.

        Topology-x-gateway collision warnings still log here so operators
        see kind drift before any submission lands. Labware rehydration
        still runs at boot because it reconstructs the pre-shutdown
        `Location.labware` graph from the persistent store, which is a
        graph-state restore (not a device dispatch) and must happen
        before any thread observes the system.
        """
        await self._rehydrate_labware_locations(note_restart_gap=not after_reload)
        self._device_controller.set_fault_listener(self._on_device_fault_changed)
        self._system.read_faults_from(self._device_controller.fault)
        self._device_controller.check_interfaces_with(
            lambda device_id: resolve_effective_interfaces(self._device_registry, device_id),
        )
        self._system.command_pipeline.report_to(self._device_controller.command_events, self, error_label)
        if not self._location_listener_registered:
            self._system.labware_location_service.add_expectation_dropped_listener(
                self._on_expectation_dropped
            )
            self._system.add_deck_reconcile_conflict_listener(
                self._on_deck_reconcile_conflict
            )
            self._location_listener_registered = True
        # Server-scoped bus, runtime-scoped listener: shutdown() unsubscribes,
        # else a rebuilt runtime's dead predecessor keeps pushing deck state.
        # Guarded separately from the unremovable location listeners so a
        # start after shutdown resubscribes.
        if not self._connection_listener_registered:
            self._connection_events.subscribe_connected(
                self._on_gateway_device_connected
            )
            self._connection_events.subscribe_disconnected(
                self._on_gateway_device_disconnected
            )
            self._connection_events.subscribe_reported(
                self._on_gateway_device_reported
            )
            self._connection_listener_registered = True
        await self._validate_topology_gateway_collisions()
        for transporter in self._system.transporters:
            transporter.bind_move_defaults(self._move_defaults_service)
        # Every mover, not just the arms: a handler's own deck gripper needs
        # the same per-labware grip height an arm does.
        for mover in self._system.movers:
            mover.bind_grip_profiles(self._grip_profile_service)
        self._state = RuntimeState.RUNNING
        if self._stall_check_interval is not None and self._stall_task is None:
            self._stall_task = asyncio.create_task(self._stall_watch_loop())

    async def _validate_topology_gateway_collisions(self) -> None:
        """Log kind drift between topology declarations and connected gateway entries.

        Kind drift is advisory: the safety contract is the interface superset
        rule enforced by the device registry (``_interface_mismatch``). Different
        kind labels can satisfy the same interface contract (a "shaker" and
        a "thermal_shaker" can both implement IShaker), so kind drift must
        not block startup if interfaces line up. Logging surfaces config
        drift to operators without refusing to boot a system that will
        dispatch correctly at the method level.

        Names present only on one side are normal: a topology declaration
        without a connection is the pre-connect state; a gateway connection
        with no declaration surfaces on the union view as ``in_topology=False``.
        """
        topology_by_name: dict[str, str] = {
            entry.name: entry.kind
            for entry in self._topology_registry.list_devices()
        }
        gateway_entries = await self._gateway_registry.list_connected()
        for gw in gateway_entries:
            expected = topology_by_name.get(gw.name)
            if expected is None:
                continue
            if expected != gw.driver_class_observed:
                logger.warning(
                    "kind drift on %s: topology declared %r, gateway observed %r "
                    "(accepting; interfaces will be checked at dispatch time)",
                    gw.name, expected, gw.driver_class_observed,
                )

    async def _rehydrate_labware_locations(self, *, note_restart_gap: bool) -> None:
        """Re-place persisted labware on the System graph at boot.

        After a crash, the labware store knows where each plate was, but the
        System graph's Location.labware slots are empty. Walk the store's
        active locations and call Location.initialize_labware so the runtime
        starts from the same physical state it shut down with.

        A store keeps identity, not the PLR object, so each labware is rebuilt
        from its template first: the deck reconciliation, ``ctx.plate()`` and
        the tip-depletion checks all read the PLR object, and a labware that
        cannot answer them is the difference between resuming and 503ing.
        """
        # The heads were unwatched for exactly as long as the labware was.
        if note_restart_gap:
            for handler in self._system.handlers_with_a_head():
                await self._system.mounted_tips.note_observation_gap(
                    handler, ObservationGapCause.RUNTIME_RESTART,
                )
        pairs = await self._labware_service.list_active_locations()
        for labware_id, position_id in pairs:
            instance = await self._labware_service.get_by_id(labware_id)
            if instance is None:
                logger.warning(
                    "Labware store referenced unknown labware_id=%r at location=%r; skipping rehydrate",
                    labware_id,
                    position_id,
                )
                continue
            location = self._system.system_map.find_gripper_location(position_id)
            if location is None:
                try:
                    location = self._system.system_map.get_location(position_id)
                except KeyError:
                    logger.warning(
                        "Labware %r persisted at unknown location=%r; skipping rehydrate",
                        labware_id,
                        position_id,
                    )
                    continue
            instance = await adopt_stored_labware(self._system, instance)
            # The runtime was off. A hand could have swapped, emptied or refilled
            # this while nothing was recording, so the number stands but stops
            # counting as confirmed until someone looks.
            if note_restart_gap:
                await self._system.labware_contents.note_observation_gap(
                    instance.ref, ObservationGapCause.RUNTIME_RESTART,
                )
            await self._system.labware_placer.arrived(instance, location, Arrival.RESTORED)

    async def _restore_labware_its_workflow_declares(self) -> None:
        """A daemon restores plates at mount, before a workflow declares their templates."""
        positions = dict(await self._labware_service.list_active_locations())
        for instance in self._system.labwares:
            position_id = positions.get(instance.id)
            if instance.has_plr_backing or position_id is None:
                continue
            try:
                self._system.get_labware_template(instance.template_name)
            except KeyError:
                continue
            restored = await adopt_stored_labware(self._system, instance)
            if restored is instance:
                continue
            location = (
                self._system.system_map.find_gripper_location(position_id)
                or self._system.system_map.get_location(position_id)
            )
            await self._system.labware_placer.arrived(restored, location, Arrival.PLACED)

    def _on_expectation_dropped(self, labware: LabwareInstance) -> None:
        """A labware that was expected and never arrived leaves nothing behind.

        The identity was minted so a thread had something to name; the thread
        gave up, so the identity goes too or it lists as labware an operator
        can see and act on. Nothing to unpersist: an expectation never
        reached the store."""
        self._system.remove_labware(labware.id)

    async def _on_gateway_device_connected(
        self, device: DeviceConnectInfo, client_id: str,
    ) -> None:
        """The device came back and may hold a new driver: its cached bring-up is forgotten and its worlds are marked.
        A background task marks labware on it stale and rebuilds a handler's world in force, unless a task holds it."""
        del client_id
        # Inline: a command can reach the returning client from the moment its devices attach.
        self._forget_agent_session(device.name)
        # Never awaited: this runs inside the websocket handshake, before that socket's receive loop starts.
        task = asyncio.create_task(self._reseed_reconnected_device(device.name))
        self._deck_reseed_tasks.add(task)
        task.add_done_callback(self._deck_reseed_tasks.discard)

    async def _on_gateway_device_disconnected(self, device_name: str) -> None:
        """The device bridge holding this device went away, so orca's belief
        that it is linked and brought up expires now rather than on reconnect:
        a command can reach the returning device bridge before its connect
        signal does."""
        self._forget_agent_session(device_name)

    async def _on_gateway_device_reported(
        self, device_name: str, reported: DeviceStatusInfo,
    ) -> None:
        """The device bridge owns the driver objects, so a report of "not
        brought up" settles it whatever orca last watched succeed. A restarted
        device bridge that reconnected to a different process reports here and
        nowhere else."""
        if not reported.observed_link.is_initialized:
            self._forget_agent_session(device_name)

    def _forget_agent_session(self, device_name: str) -> None:
        """Drop what orca cached about a device whose device bridge rebuilt its
        drivers.

        Both layers, or the device only half recovers: the driver's own flag is
        what `Transporter.ensure_initialized` reads before a pick, and the
        system's world record is what gates the bring-up walk for everything
        that has no such per-use check. Every world it holds is marked, so the
        next command to it rebuilds its world from the record.
        """
        self._devices.forget_driver_session(device_name)
        self._system.forget_bringup(device_name)
        self._system.device_worlds.mark_every_world(device_name)

    async def _reseed_reconnected_device(self, device_name: str) -> None:
        try:
            await self._note_gap_for_labware_on(
                device_name, ObservationGapCause.DEVICE_RECONNECT,
            )
            await self._devices.reseed_deck_if_liquid_handler(device_name)
        except Exception:
            logger.exception(
                "Deck reseed after reconnect failed for device %r", device_name,
            )

    async def switch_device_mode(self, device_name: str, mode: DeviceMode, *, confirm: bool) -> ModeSwitch:
        if not await self._device_modes.would_change(device_name, mode):
            return ModeSwitch(switched=False)
        async with self._device_mode_lock:
            runs = [
                RunInProgress(execution_id=e.id, workflow_name=e.workflow_name)
                for e in self._executions.values() if not e.task.done()
            ]
            if runs:
                raise ModeSwitchDuringRunError(device_name, runs)
            busy = self._system.device_in_use_by(device_name)
            if busy is not None:
                raise DeviceLockedError(f"{device_name}: {busy}. Switch it once that finishes.")
            warning = mode_switch_warning(device_name, mode)
            if not confirm:
                return ModeSwitch(switched=False, warning=warning)
            # STALE before the switch: a flag with no switch only asks for a second look; a switch with no flag trusts unwatched state.
            await self._note_gap_for_labware_on(device_name, ObservationGapCause.MODE_SWITCHED)
            await self._device_modes.set(device_name, mode)
            self._forget_agent_session(device_name)
            try:
                await self._system.bring_up_device(device_name)
            except Exception as exc:
                cause = exc.cause if isinstance(exc, DeviceInitializationError) else exc
                failure = bring_up_error(device_name, mode, cause)
                logger.warning("%s", failure, exc_info=True)
                self._device_controller.mark_not_brought_up(device_name, failure)
                return ModeSwitch(switched=True, warning=warning, bring_up_error=failure)
            return ModeSwitch(switched=True, warning=warning)

    async def _note_gap_for_labware_on(
        self, device_name: str, cause: ObservationGapCause,
    ) -> None:
        """Mark everything standing on this device as worth a second look.

        Orca did not watch the device through ``cause``. The numbers stand, since they are
        still the best there is, but they stop counting as confirmed.
        """
        # The head a handler carries is as unwatched as the labware standing on it.
        if device_name in self._system.handlers_with_a_head():
            await self._system.mounted_tips.note_observation_gap(device_name, cause)
        for instance in self._system.labwares:
            location = self._labware_location_of(instance)
            if location is None or location.owner_mutex_id != device_name:
                continue
            await self._system.labware_contents.note_observation_gap(instance.ref, cause)

    def _labware_location_of(self, instance: LabwareInstance) -> Location | None:
        try:
            return self._system.labware_location_service.get(instance)
        except KeyError:
            return None

    async def flush_deck_reseeds(self) -> None:
        """Wait for every in-flight reconnect reseed to finish.

        A run's bring-up waits here so its initialize does not meet a reseed's command on the wire. A reseed skips a
        device another task holds, so this wait is one deck rebuild at most, never a run's long command."""
        while self._deck_reseed_tasks:
            await asyncio.gather(
                *list(self._deck_reseed_tasks), return_exceptions=True,
            )

    def _on_deck_reconcile_conflict(self, conflict: DeckReconcileConflict) -> None:
        """The ledger and a deck disagree and nothing can settle it but a human,
        so it goes on the incident surface. Neither side is changed."""
        self._incident_service.record(
            category=IncidentCategory.DECK_RECONCILE_CONFLICT,
            severity=IncidentSeverity.ERROR,
            message=_deck_conflict_message(conflict),
            detail=DeckReconcileConflictDetail(
                device_name=conflict.device_name,
                labware_id=conflict.labware_id,
                labware_name=conflict.labware_name,
                position_id=conflict.position_id,
                reason=conflict.reason.value,
                driver_site=conflict.driver_site,
                blocking_labware_name=conflict.blocking_labware_name,
                detail=conflict.detail,
            ),
            recovery_action=RecoveryAction.NONE,
        )

    async def shutdown(self, *, confirm: bool = False) -> None:
        """Abort all active executions, stop tick loops, and shut down.

        `confirm` accepted for ISystemRuntime protocol conformance; the
        operator guard lives at the CLI/REST boundary.
        """
        del confirm
        self._device_controller.clear_fault_listener(self._on_device_fault_changed)
        self._connection_events.unsubscribe_connected(self._on_gateway_device_connected)
        self._connection_events.unsubscribe_disconnected(
            self._on_gateway_device_disconnected
        )
        self._connection_events.unsubscribe_reported(self._on_gateway_device_reported)
        self._connection_listener_registered = False
        for reseed_task in list(self._deck_reseed_tasks):
            reseed_task.cancel()
        if self._deck_reseed_tasks:
            await asyncio.gather(
                *list(self._deck_reseed_tasks), return_exceptions=True,
            )
            self._deck_reseed_tasks.clear()
        if self._stall_task is not None:
            self._stall_task.cancel()
            try:
                await self._stall_task
            except asyncio.CancelledError:
                pass
            self._stall_task = None
        await self._drain_live_executions()
        await self._wait_for_stop_records()
        for wf in self._executing_workflows:
            wf.stop_tick_loop()
        for wf in self._executing_workflows:
            await wf.await_tick_loop_stopped()
        self._executing_workflows.clear()
        # The orphan-slot drain and the failed-execution teardown, both of
        # which write to stores their owner closes once shutdown returns.
        if self._background_tasks:
            for background in list(self._background_tasks):
                background.cancel()
            await _bounded(
                asyncio.gather(*self._background_tasks, return_exceptions=True),
                _SHUTDOWN_DRAIN_TIMEOUT_S,
                "background teardown tasks",
            )
            self._background_tasks.clear()
        # Last of the drains: a teardown cancelled just above can schedule one
        # more cancel on its way out.
        await _bounded(
            self._device_controller.await_cancels_sent(),
            _CANCEL_DRAIN_TIMEOUT_S,
            "cancels still on their way to the device bridges",
        )
        # Before the services close, and before anything rebuilds from the database: a position still being saved lands.
        await self._system.labware_placer.finish_saves(_SHUTDOWN_DRAIN_TIMEOUT_S)
        # The services outlive the runtime and keep their drains. What this shutdown queued is saved before it returns.
        await _bounded(
            self._save_what_shutdown_queued(),
            _SHUTDOWN_DRAIN_TIMEOUT_S,
            "the incidents and execution records this shutdown queued to be saved",
        )
        self._unsaved_records.remove_run_pauser(self._pause_for_an_unsaved_record)
        self._state = RuntimeState.STOPPED

    async def _save_what_shutdown_queued(self) -> None:
        await self._incident_service.flush_pending()
        await self._execution_record_service.flush_pending()

    async def _wait_for_stop_records(self) -> None:
        """A stop's records are written beside it, and the stores close after shutdown: wait for them, up to the limit."""
        not_saved = await self._system.command_pipeline.stop_reports_written(_SHUTDOWN_DRAIN_TIMEOUT_S)
        if not_saved:
            logger.error(
                "Shutdown could not save, within %.0fs, the records of stopped commands; continuing. Not saved: the "
                "not-observed flags and the stop incident for %s.",
                _SHUTDOWN_DRAIN_TIMEOUT_S, ", ".join(not_saved),
            )

    async def _drain_live_executions(self) -> None:
        """Stop every live execution and wait for it, before anything closes.

        Cancelling only ``execution.task`` leaves the workflow's start task and
        its thread tasks running, because they are detached, and they keep
        working against stores their owner closes once shutdown returns.
        """
        await _bounded(
            self._abort_and_await_executions(),
            _SHUTDOWN_DRAIN_TIMEOUT_S,
            "running executions",
        )

    async def _abort_and_await_executions(self) -> None:
        """The whole drain, bounded by its caller.

        ``abort_execution`` is inside the bound, not outside it: it gathers the
        thread tasks it cancels, and a thread with a long unwind makes that wait
        as long as the unwind.
        """
        for execution_id, execution in list(self._executions.items()):
            if execution.task.done():
                continue
            await self.abort_execution(execution_id)
            if not _phase_to_state(execution.phase).is_terminal():
                execution.phase = ExecutionPhase.ABORTED
        live = [e.task for e in self._executions.values() if not e.task.done()]
        if live:
            await asyncio.gather(*live, return_exceptions=True)

    def _liquid_handlers(self) -> list[LiquidHandler]:
        return [d for d in self._system.devices if isinstance(d, LiquidHandler)]

    async def _stall_watch_loop(self) -> None:
        """Backstop for co-labware waits the reservation-cycle detector cannot see.

        Co-labware waits are unbounded, so a genuine convergence deadlock or an
        orphaned wait (a contributor that died) would otherwise hang forever. Each
        execution is evaluated INDEPENDENTLY -- one healthy execution must never
        mask a stalled sibling -- see ``stall_detector`` for why the rule is
        timing-independent and never fires on a healthy slow run. A stalled
        execution is paused so the operator can recover instead of hanging.
        """
        assert self._stall_check_interval is not None
        while True:
            try:
                await asyncio.sleep(self._stall_check_interval)
            except asyncio.CancelledError:
                return
            if self._state is not RuntimeState.RUNNING:
                return
            try:
                self._check_for_stalls()
            except Exception:
                # A detector bug must never take down the runtime; the next tick
                # re-evaluates from a fresh snapshot.
                logger.exception("Stall detector tick failed")

    def _check_for_stalls(self) -> None:
        live_ids = set(self._executions)
        for gone in self._stall_detectors.keys() - live_ids:
            del self._stall_detectors[gone]
        snapshots_by_id = {
            execution_id: self._build_execution_snapshots(execution_id)
            for execution_id in live_ids
        }
        # One in-flight thread ANYWHERE keeps reservation waits out of the
        # stall shape for every execution (see StallDetector.evaluate).
        system_quiescent = not any(
            snapshot_in_flight(snapshot)
            for snapshots in snapshots_by_id.values()
            for snapshot in snapshots
        )
        for execution_id in live_ids:
            detector = self._stall_detectors.setdefault(
                execution_id, StallDetector(self._stall_required_stable_ticks),
            )
            report = detector.evaluate(
                snapshots_by_id[execution_id], system_quiescent=system_quiescent,
            )
            if report is not None:
                self._handle_stall(execution_id, report)

    def _build_execution_snapshots(
        self, execution_id: str,
    ) -> list[ThreadStallSnapshot]:
        return [
            ThreadStallSnapshot(
                thread_id=thread.id,
                status=thread.status,
                waiting_on=_stall_wait_subject(thread),
                following_peer_action=thread.following_peer_action,
            )
            for thread in self._get_execution_threads(execution_id)
        ]

    def _end_stall_episode(self, execution_id: str) -> None:
        execution = self._executions.get(execution_id)
        if execution is not None and execution.stall_event.is_set():
            execution.stall_event.clear()
            execution.stall_report = None
            self._stall_detectors.pop(execution_id, None)

    def _handle_stall(self, execution_id: str, report: StallReport) -> None:
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
            SystemStallDetail,
        )
        logger.error(f"System stall in execution {execution_id}: {report.detail}")
        incident = self._incident_service.record(
            category=IncidentCategory.SYSTEM_STALL,
            severity=IncidentSeverity.ERROR,
            message=(
                f"System stall: {len(report.thread_ids)} threads in this execution "
                f"are internally blocked (awaiting co-labware or a reservation) "
                f"with none in flight, so no delivery or release can occur. "
                f"{report.detail}"
            ),
            detail=SystemStallDetail(
                stalled_thread_ids=report.thread_ids, waits=report.waits,
            ),
            recovery_action=RecoveryAction.RESTART_EXECUTION,
            execution_id=execution_id,
        )
        # Execution-level pause, not just threads: the latch also refuses
        # JOIN_EXISTING submissions into the stalled execution until resume.
        self.pause_execution(execution_id, reason="system", message=incident.message)
        execution = self._executions.get(execution_id)
        if execution is not None:
            # Report before event: wait_for_execution reads the report when
            # the event wakes it.
            execution.stall_report = report
            execution.stall_event.set()

    async def submit(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup] = (),
        variables: Mapping[str, OptionValue] | None = None,
        batch_mode: BatchMode = BatchMode.STANDALONE,
        operator_id: str | None = None,
        deployment_profile: str | None = None,
        mode: WorkflowRunMode | None = None,
        sim_speedup: float | None = None,
        every_variable_is_read: bool = True,
    ) -> Submission:
        """Accept a submission of labware groups for a workflow execution.

        Two modes, discriminated by whether ``groups`` is empty:

        - **Groupless** (``groups=()``): legacy one-shot. Each call always
          creates a fresh Execution, matching single-run semantics.
        - **Grouped + JOIN_EXISTING**: multi-submission. If an
          ACCEPTING execution for ``workflow.name`` exists, the new
          submission's entry threads inject into that execution; if the
          existing execution is DRAINING, the call raises (resubmit as
          STANDALONE to start fresh). With no live execution, falls
          through to a fresh build.
        - **Grouped + STANDALONE**: ALWAYS boots a fresh execution per
          the wire contract documented on a hosted deployment's REST surface. Even when an ACCEPTING
          execution for the same workflow exists, STANDALONE does NOT
          join it; it always starts a new execution. ``batch_mode`` is
          inspected on both the ACCEPTING and DRAINING branches so
          STANDALONE never silently joins an alive ACCEPTING execution.

          A STANDALONE submission against a workflow whose existing
          execution is DRAINING also boots fresh, but with a parallel-
          execution implication worth flagging: the original DRAINING
          execution's threads continue to completion AND the new
          ACCEPTING execution starts running in parallel. The
          ``_active_executions[workflow.name]`` index gets overwritten
          to the new execution, so subsequent JOIN_EXISTING submissions
          target the new one (the DRAINING one is no longer reachable
          by name lookup, only by execution_id). Operators who close an
          execution explicitly and then submit STANDALONE should expect
          this -- two in-flight executions for the same workflow,
          consuming resources until the DRAINING one drains. To avoid
          parallelism, wait for the DRAINING execution to terminate
          before submitting STANDALONE.
        """
        if self._state != RuntimeState.RUNNING:
            raise RuntimeError("Runtime is not running")

        # run_mode is required; there is no deployment fallback.
        if mode is None:
            raise RunModeRequiredError()
        _check_sim_speedup_applies(mode, sim_speedup)
        if batch_mode is BatchMode.JOIN_EXISTING and sim_speedup is not None:
            raise SimSpeedupNotApplicableError(
                "the speed-up belongs to the execution a JOIN_EXISTING submission "
                "joins; change that execution's own speed-up instead",
            )

        # 1b. A system runs in one run mode at a time.
        blocker = self._find_other_run_mode(mode)
        if blocker is not None:
            raise OtherRunModeActiveError(
                blocking_execution_id=blocker.id,
                blocking_workflow_name=blocker.workflow_name,
                existing_run_mode=blocker.submissions[0].run_mode,
                submitted_run_mode=mode,
            )

        profile = await self._profile_named(deployment_profile)
        self._validate_group_coverage(workflow, groups)
        self._refuse_bad_inputs(
            workflow, variables, profile, self._join_target(workflow, groups, batch_mode),
            every_variable_is_read=every_variable_is_read,
        )
        # Hold the join target's slot closes across the whole injection window
        # (resolve + build/register); see ExecutingWorkflow.injecting().
        async with self._injection_guard(self._join_target(workflow, groups, batch_mode)):
            resolved_acquisitions = await self._resolve_acquisitions(workflow, groups)
            self._refuse_naming_a_partner_every_group_shares(workflow, groups)
            await self._refuse_resident_racks_with_no_stated_tips(
                workflow, self._join_target(workflow, groups, batch_mode),
            )
            # A groupless submission stays groupless for joining and indexing; only the run carries the made-up group.
            run_groups, adopted = self._adopt_unused_plates_on_start_locations(workflow, groups, resolved_acquisitions)
            self._validate_start_locations(
                workflow, run_groups, resolved_acquisitions, self._join_target(workflow, groups, batch_mode),
            )

            existing = self._active_executions.get(workflow.name)
            # `task.done()` settles after the run has stopped taking work, so it
            # alone would let a join land on a run already finishing.
            existing_alive = (
                existing is not None
                and not existing.task.done()
                and (
                    existing.executing_workflow is None
                    or existing.executing_workflow.is_accepting_injections
                )
            )
            # JOIN_EXISTING is opt-in; STANDALONE always boots fresh, and
            # DRAINING/STOPPING + JOIN_EXISTING is rejected so the operator knows.
            if (
                groups and existing is not None and existing_alive
                and batch_mode is BatchMode.JOIN_EXISTING
            ):
                if existing.phase is ExecutionPhase.ACCEPTING:
                    # A paused execution stays ACCEPTING but its pause latch gates
                    # new work: refuse the join rather than queue behind the pause.
                    if existing.is_paused:
                        raise SubmissionToPausedExecutionError(
                            blocking_execution_id=existing.id,
                            blocking_workflow_name=existing.workflow_name,
                        )
                    # A BATCHABLE join would route straight into a quarantined
                    # slot, whose accept-partial resume discards the backlog.
                    if (
                        existing.executing_workflow is not None
                        and existing.executing_workflow.has_orphaned_slots()
                    ):
                        raise SubmissionBlockedByOrphanedBacklogError(
                            blocking_execution_id=existing.id,
                            blocking_workflow_name=existing.workflow_name,
                        )
                    # One execution shares one per-task run-mode seed; mixing modes
                    # would misroute driver dispatch, so the modes must match.
                    if existing.submissions:
                        existing_run_mode = existing.submissions[0].run_mode
                        if existing_run_mode is not mode:
                            raise RunModeMismatchError(
                                blocking_execution_id=existing.id,
                                blocking_workflow_name=existing.workflow_name,
                                existing_run_mode=existing_run_mode,
                                submitted_run_mode=mode,
                            )
                    _refuse_a_second_profile(existing, deployment_profile)
                    await self._refuse_a_join_unless_its_devices_are_reachable(mode)
                    execution = existing
                    inject = True
                elif existing.phase in (
                    ExecutionPhase.DRAINING, ExecutionPhase.STOPPING,
                ):
                    raise RuntimeError(
                        f"Execution '{existing.id}' for workflow '{workflow.name}' "
                        f"is {existing.phase.value.upper()}; JOIN_EXISTING "
                        f"submissions are rejected. Resubmit as STANDALONE to "
                        f"start a new execution, or wait for this one to terminate."
                    )
                else:
                    execution, inject = await self._start_a_fresh_run(workflow, groups, mode, sim_speedup)
            else:
                execution, inject = await self._start_a_fresh_run(workflow, groups, mode, sim_speedup)

            submission = Submission(
                id=str(uuid.uuid4()),
                execution_id=execution.id,
                workflow_name=workflow.name,
                groups=tuple(run_groups),
                variables=dict(variables) if variables else {},
                batch_mode=batch_mode,
                submitted_at=datetime.now(timezone.utc),
                run_mode=mode,
                operator_id=operator_id,
                deployment_profile=deployment_profile,
                status=SubmissionStatus.ACCEPTED,
                resolved_acquisitions=resolved_acquisitions,
            )
            execution.submissions.append(submission)
            self._plates_used_by_runs |= adopted

            if inject:
                await self._inject_submission(execution, workflow, submission, batch_mode)
            else:
                # No await since the run task was created, so no thread has read a variable, and a read
                # right after the submit finds the run.
                self._system.variable_store.create_execution(execution.id, workflow.name)
                if profile is not None:
                    self._system.variable_store.load_profile(execution.id, profile)

        return submission

    async def _profile_named(self, name: str | None) -> DeploymentProfile | None:
        if name is None:
            return None
        profile = await self._profile_store.get(name)
        if profile is None:
            raise DeploymentProfileNotFoundError(name)
        return profile

    def _refuse_bad_inputs(
        self,
        workflow: WorkflowTemplate,
        variables: Mapping[str, OptionValue] | None,
        profile: DeploymentProfile | None,
        join_target: Execution | None,
        *,
        every_variable_is_read: bool,
    ) -> None:
        store = self._system.variable_store
        refusals = refusals_for(SubmittedInputs(
            workflow_name=workflow.name,
            inline=variables or {},
            profile=profile.variables if profile is not None else {},
            workflow_definitions=store.workflow_definitions(workflow.name),
            global_definitions=store.global_definitions(),
            computed=store.computed_names() | frozenset(profile.computed if profile is not None else ()),
            every_variable_is_read=every_variable_is_read,
            global_values=store.global_value_names(),
            set_on_joined_execution=(
                store.execution_value_names(join_target.id)
                if join_target is not None else frozenset()
            ),
        ))
        if refusals:
            raise SubmissionInputsRefusedError(refusals)

    def _pacer_for_new_execution(
        self, mode: WorkflowRunMode, sim_speedup: float | None,
    ) -> SimPacer | None:
        """A sim run gets a pacer at the asked-for speed-up or the deployment default.
        LIVE and check runs get none, so no step of them is ever paced."""
        if mode is not WorkflowRunMode.SIM:
            return None
        return SimPacer(self._default_sim_speedup if sim_speedup is None else sim_speedup)

    async def _start_a_fresh_run(
        self, workflow: WorkflowTemplate, groups: Sequence[LabwareGroup],
        mode: WorkflowRunMode, sim_speedup: float | None,
    ) -> tuple[Execution, bool]:
        # One lock with switch_device_mode, so a device never changes mode between bring-up and the run existing.
        async with self._device_mode_lock:
            await self._refuse_unless_its_devices_are_ready(mode)
            return self._build_fresh_execution(workflow, groups, mode=mode, sim_speedup=sim_speedup)

    def _build_fresh_execution(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup],
        *,
        mode: WorkflowRunMode,
        sim_speedup: float | None,
    ) -> tuple[Execution, bool]:
        """Create a new Execution + task for ``workflow``; register it.

        Sets the active-executions index for this workflow name (overwriting
        any prior DRAINING/terminal entry) when ``groups`` is non-empty. Used
        by submit() for both the first-submit path and the
        STANDALONE-after-close path.

        Returns (execution, inject=False) so callers can fall through to the
        common submission-wiring block.

        **Sync-invariant**: the call chain from
        ``submit()`` through this helper and back to the caller's
        ``execution.submissions.append(submission)`` MUST remain
        synchronous (no ``await`` between the ``asyncio.create_task``
        below and that append). ``_run_workflow`` and
        ``_spawn_thread_in_execution`` read ``execution.submissions[0]``
        directly with no fallback; adding an ``await`` in this window
        would let the task scheduled below run before the submission
        lands, producing ``IndexError`` on the first read.
        """
        execution_id = str(uuid.uuid4())
        task = asyncio.create_task(self._run_workflow(execution_id, workflow))
        execution = Execution(
            id=execution_id,
            workflow_name=workflow.name,
            workflow=workflow,
            system=self._system,
            task=task,
            sim_pacer=self._pacer_for_new_execution(mode, sim_speedup),
        )
        self._executions[execution_id] = execution
        # Known to the forwarder from here: an operator's change between submit
        # and the run task's start must still reach every sink.
        self._forwarder.register_execution(execution_id)
        if groups:
            self._active_executions[workflow.name] = execution
        # The forwarder lets go of the run once its end is recorded and its end event has fired.
        task.add_done_callback(lambda _t: self._on_task_done(execution_id))
        return execution, False

    def _find_other_run_mode(
        self, mode: WorkflowRunMode,
    ) -> Execution | None:
        """A running execution under a run mode other than `mode`, or None."""
        for execution in self._executions.values():
            if execution.task.done() or not execution.submissions:
                continue
            if execution.submissions[0].run_mode is not mode:
                return execution
        return None

    def system_run_mode(self) -> WorkflowRunMode | None:
        """The run mode the system is in: the running execution's, else the last one's; None before any run."""
        started = [e for e in self._executions.values() if e.submissions]
        running = [e for e in started if not e.task.done()]
        chosen = running or started
        return chosen[-1].submissions[0].run_mode if chosen else None

    def _active_execution(self) -> Execution | None:
        """The unfinished run, running or paused; None when no run is active."""
        for execution in self._executions.values():
            if not execution.task.done() and execution.submissions:
                return execution
        return None

    def active_run_mode(self) -> WorkflowRunMode | None:
        """The mode of the unfinished run, running or paused; None when no run is active."""
        execution = self._active_execution()
        return None if execution is None else execution.submissions[0].run_mode

    def operator_base_mode(self) -> WorkflowRunMode:
        """While a run is active, running or paused, a command to a device goes in the run's mode, not the device's own setting.

        A system runs in one mode at a time, and a thread does not declare the
        devices it uses, so this covers every device in the system.
        """
        return self.active_run_mode() or OUT_OF_RUN_BASE

    def operator_sim_pacer(self) -> SimPacer:
        """What an operator's simulated command waits at: the active run's speed-up, else this server's default."""
        execution = self._active_execution()
        if execution is not None and execution.sim_pacer is not None:
            return execution.sim_pacer
        return SimPacer(self._default_sim_speedup)

    def _join_target(
        self, workflow: WorkflowTemplate, groups: Sequence[LabwareGroup],
        batch_mode: BatchMode,
    ) -> Execution | None:
        """The ACCEPTING execution a JOIN_EXISTING submission will inject into, or
        None. Sync, so it can be read before the submit path first awaits.

        The guard reads this pre-await while the injection target is re-read
        post-resolve; the two diverge only if a concurrent submit overwrites the
        active-executions index during a barcode-resolve yield (pre-existing,
        bounded to that yield; a submit-serialization fix is a separate follow-up).
        """
        if not groups or batch_mode is not BatchMode.JOIN_EXISTING:
            return None
        existing = self._active_executions.get(workflow.name)
        if existing is None or existing.task.done():
            return None
        if existing.phase is not ExecutionPhase.ACCEPTING:
            return None
        if existing.executing_workflow is None:
            return None
        if not existing.executing_workflow.is_accepting_injections:
            return None
        return existing

    def _injection_guard(
        self, execution: Execution | None,
    ) -> AbstractAsyncContextManager[None]:
        """Hold the target execution's slot closes across an injection window; a
        no-op context when there is no injection target."""
        if execution is None or execution.executing_workflow is None:
            return nullcontext()
        return execution.executing_workflow.injecting()

    async def _inject_submission(
        self,
        execution: Execution,
        workflow: WorkflowTemplate,
        submission: Submission,
        batch_mode: BatchMode,
    ) -> None:
        """Deliver a second (or later) submission's groups to a live execution.

        Waits for the ExecutingWorkflow to be attached, builds new entry
        threads tagged with this submission's id/groups/batch_mode, registers
        them in the system's ThreadRegistry, and hands them to the running
        ExecutingWorkflow via add_entry_threads.
        """
        await execution.workflow_attached.wait()
        if execution.task.done():
            raise RuntimeError(
                f"Cannot inject submission {submission.id}: execution "
                f"{execution.id} ({workflow.name}) already finished"
            )
        assert execution.executing_workflow is not None

        # Write injected submission's variables to its own partition so they
        # don't override sibling submissions' overrides in the same execution.
        if submission.variables:
            for var_name, value in submission.variables.items():
                self._system.variable_store.set_submission(
                    var_name, value, execution.executing_workflow.id, submission.id,
                )

        new_entry_threads = await self._system.build_entry_threads_for(
            workflow, submission.id, submission.groups, batch_mode=batch_mode,
            resolved_acquisitions=submission.resolved_acquisitions,
            run_mode=submission.run_mode,
        )
        for thread in new_entry_threads:
            self._system.add_thread(thread)
        execution.executing_workflow.add_entry_threads(new_entry_threads)

        # SUBMISSION.ACCEPTED for the injected submission.
        self._accept_submission(
            execution.executing_workflow.id, workflow.name, submission,
        )
        # Threads just registered; submission is active from this point.
        submission.status = SubmissionStatus.IN_PROGRESS

    def close_execution(self, execution_id: str) -> ExecutionPhase:
        """Transition ``execution_id`` from ACCEPTING to DRAINING.

        After this call, JOIN_EXISTING submissions against this execution
        are rejected. STANDALONE submissions for the same workflow_name
        boot a fresh execution (ACCEPTING) which replaces this one in the
        active-executions index. In-flight threads inside the draining
        execution continue to completion; once all terminate the execution
        transitions to COMPLETED (or FAILED/ABORTED on error).

        The execution entry stays in `_active_executions` with phase DRAINING
        so submit() can distinguish "is draining" from "does not exist" when
        rejecting JOIN_EXISTING.

        Idempotent: returns the current phase without mutation if the
        execution is already DRAINING or terminal.
        """
        execution = self._executions.get(execution_id)
        if execution is None:
            raise KeyError(f"No execution with id '{execution_id}'.")
        if execution.phase is ExecutionPhase.ACCEPTING:
            execution.phase = ExecutionPhase.DRAINING
        return execution.phase

    def _validate_group_coverage(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup],
    ) -> None:
        """Enforce the group coverage rules at submit.

        Rules (at submit time, before accepting):
        - Group missing a member for a PER_GROUP + required=True entry thread
          template => reject.
        - Submission missing a member for a SHARED_ACROSS_GROUPS + required=True
          entry thread template => reject.
        - Member references a thread_template_name not registered in the
          workflow (neither as entry thread nor as an auto-spawn template)
          => reject.
        - Auto-spawn-only templates with no member => accept (spawned reactively).
        - ``required=False`` entry threads may be omitted.

        Does nothing when ``groups`` is empty: groupless submits are the
        legacy one-shot path and don't require coverage validation.
        """
        if not groups:
            return

        valid_names: set[str] = set()
        for t in [*workflow.entry_thread_templates, *workflow.auto_spawn_registry.values()]:
            valid_names.update(t.names)

        for group in groups:
            for member in group.members:
                if member.thread_template_name not in valid_names:
                    raise AcquisitionValidationError(
                        f"Group '{group.id}' member references unknown "
                        f"thread_template_name '{member.thread_template_name}'. "
                        f"Known: {sorted(valid_names)}"
                    )

        named_by = {group.id: threads_a_group_names(group, workflow.thread_templates) for group in groups}
        for template in workflow.entry_thread_templates:
            if not template.required:
                continue
            sharing = template.labware_template.group_sharing
            if sharing is GroupSharing.PER_GROUP:
                for group in groups:
                    if template.name not in named_by[group.id]:
                        raise AcquisitionValidationError(
                            f"Group '{group.id}' is missing a member for required "
                            f"PER_GROUP thread template '{template.name}'."
                        )
            else:
                any_member = any(template.name in named for named in named_by.values())
                if not any_member:
                    raise AcquisitionValidationError(
                        f"Submission is missing a member for required "
                        f"SHARED_ACROSS_GROUPS thread template '{template.name}'."
                    )

    def _safe_emit(self, event_name: str, context: ExecutionContext) -> None:
        self._event_bus.emit(event_name, context)

    def _accept_submission(
        self, workflow_instance_id: str, workflow_name: str, submission: Submission,
    ) -> None:
        """Record the run as RUNNING, then fire SUBMISSION.{id}.ACCEPTED on the workflow event bus."""
        self._execution_record_service.record_running(
            execution_id=workflow_instance_id,
            workflow_name=workflow_name,
            submitted_at=datetime.now(timezone.utc),
        )
        self._safe_emit(
            f"SUBMISSION.{submission.id}.ACCEPTED",
            SubmissionExecutionContext(
                execution_id=workflow_instance_id,
                workflow_name=workflow_name,
                submission_id=submission.id,
                group_count=len(submission.groups),
            ),
        )

    def _emit_submission_terminal(
        self,
        workflow_instance_id: str,
        workflow_name: str,
        submission: Submission,
        status: str,
        reason: str | None = None,
    ) -> None:
        """Fire SUBMISSION.{id}.{COMPLETED|PARTIAL_COMPLETED|FAILED|ABORTED} as the execution
        terminates. One emission per submission per execution."""
        self._safe_emit(
            f"SUBMISSION.{submission.id}.{status}",
            SubmissionExecutionContext(
                execution_id=workflow_instance_id,
                workflow_name=workflow_name,
                submission_id=submission.id,
                group_count=len(submission.groups),
                reason=reason,
            ),
        )

    def _emit_execution_terminal(
        self,
        workflow_instance_id: str,
        workflow_name: str,
        execution: Execution,
        status: str,
    ) -> None:
        """Fire EXECUTION.{id}.{COMPLETED|PARTIAL_COMPLETED|FAILED|ABORTED}."""
        self._safe_emit(
            f"EXECUTION.{execution.id}.{status}",
            ExecutionLifecycleContext(
                execution_id=execution.id,
                workflow_name=workflow_name,
                reason=execution.error,
            ),
        )

    @staticmethod
    def _refuse_naming_a_partner_every_group_shares(
        workflow: WorkflowTemplate, groups: Sequence[LabwareGroup],
    ) -> None:
        """One group cannot choose the labware of a partner thread that other groups or submissions also use."""
        entry_templates = workflow.entry_thread_templates
        partners = [t for t in workflow.thread_templates if not any(t is entry for entry in entry_templates)]
        for group in groups:
            for member in group.members:
                if not isinstance(member.acquisition, BarcodeAcquisition):
                    continue
                partner = thread_named(partners, member.thread_template_name)
                if partner is None:
                    continue
                labware_template = partner.labware_template
                if (
                    partner.start_reuse_existing
                    or labware_template.group_sharing is GroupSharing.SHARED_ACROSS_GROUPS
                    or labware_template.submission_batching is SubmissionBatching.BATCHABLE
                ):
                    raise AcquisitionValidationError(
                        f"Group '{group.id}' named barcode '{member.acquisition.barcode}' for "
                        f"'{member.thread_template_name}', and that thread's labware is shared by every "
                        f"group or submission, so one group cannot choose its labware. Leave it off the "
                        f"group, or name the partner from the workflow with set_partner_constraint."
                    )

    async def _resolve_acquisitions(
        self, workflow: WorkflowTemplate, groups: Sequence[LabwareGroup],
    ) -> dict[tuple[str, str], ResolvedAcquisition]:
        """Validate and resolve each group member's Acquisition at submit time.

        - BarcodeAcquisition: find the barcode in the labware service, then bind
          the system's live instance with that id; raise if either is missing.
        - LocationAcquisition: resolve the source_location string to a Location
          object via the system topology; raise if missing.
        - PoolAcquisition: no resolution needed (the pool is consulted at
          thread start).

        Returns a dict keyed by (group_id, thread_template_name) that the
        ThreadFactory reads synchronously during entry-thread construction.
        Members with PoolAcquisition are omitted from the dict; the factory
        treats missing keys as "pool-sourced".
        """
        resolved: dict[tuple[str, str], ResolvedAcquisition] = {}
        named_by: dict[str, str] = {}
        held_by = self._labware_in_use()
        for group in groups:
            for member in group.members:
                acquisition = member.acquisition
                key = (group.id, member.thread_template_name)
                if isinstance(acquisition, BarcodeAcquisition):
                    found = await self._labware_service.get_by_barcode(acquisition.barcode)
                    if found is None:
                        raise AcquisitionValidationError(
                            f"Group '{group.id}' member '{member.thread_template_name}' "
                            f"requested BarcodeAcquisition(barcode='{acquisition.barcode}') "
                            f"but no labware with that barcode is registered in the store."
                        )
                    # The store keeps identity only; the thread needs the instance the system holds.
                    live = next((lw for lw in self._system.labwares if lw.id == found.id), None)
                    if live is None:
                        raise AcquisitionValidationError(
                            f"Group '{group.id}' member '{member.thread_template_name}' "
                            f"requested BarcodeAcquisition(barcode='{acquisition.barcode}') "
                            f"but labware {found.id} holding it is not in the running system: "
                            f"it was discharged or cleared, and a barcode is never reused."
                        )
                    label = f"group '{group.id}' member '{member.thread_template_name}'"
                    template = _entry_thread_named(workflow, member.thread_template_name)
                    if template is not None:
                        facts = await self._member_facts(live, template, named_by.get(live.id), held_by.get(live.id))
                        refusal = refusal_for(facts)
                        if refusal is not None:
                            raise AcquisitionValidationError(
                                f"{label.capitalize()} names barcode '{acquisition.barcode}': {refusal}"
                            )
                    named_by[live.id] = label
                    resolved[key] = ResolvedAcquisition(labware_instance=live)
                elif isinstance(acquisition, LocationAcquisition):
                    try:
                        loc = self._system.get_location(acquisition.source_location)
                    except KeyError as e:
                        raise AcquisitionValidationError(
                            f"Group '{group.id}' member '{member.thread_template_name}' "
                            f"requested LocationAcquisition(source_location="
                            f"'{acquisition.source_location}') but that location is not "
                            f"in the system topology."
                        ) from e
                    resolved[key] = ResolvedAcquisition(start_location=loc)
                # PoolAcquisition: no entry in resolved dict
        return resolved

    def _labware_in_use(self, only: Execution | None = None) -> dict[str, str]:
        """Labware a running execution holds, by id, with who holds it. A submission's claim counts
        from acceptance: its threads are built later, in the execution's own task."""
        in_use: dict[str, str] = {}
        for detail in self.blocker_execution_details():
            if only is not None and detail.id != only.id:
                continue
            finished = {
                t.labware_id for t in detail.threads
                if LabwareThreadStatus(t.status).is_terminal()
            }
            for thread in detail.threads:
                if thread.labware_id is not None and thread.labware_id not in finished:
                    in_use[thread.labware_id] = f"thread {thread.name}"
            for submission in self._executions[detail.id].submissions:
                for resolved in submission.resolved_acquisitions.values():
                    claimed = resolved.labware_instance
                    if claimed is not None and claimed.id not in finished:
                        in_use.setdefault(claimed.id, f"execution {detail.id}")
        return in_use

    async def _tip_layout_is_unknown(self, labware: LabwareInstance) -> bool:
        return (
            isinstance(labware, TipRackInstance)
            and not (await self._system.labware_contents.of(labware.ref)).is_known
        )

    async def _refuse_resident_racks_with_no_stated_tips(
        self, workflow: WorkflowTemplate, join_target: Execution | None,
    ) -> None:
        # Exempt: racks the joined run's unfinished threads or accepted submissions hold. A rack it is still binding
        # has no thread yet and is not exempt; the bind writes its tips before the rack shows on its site.
        in_use = self._labware_in_use(only=join_target) if join_target is not None else {}
        for template in workflow.auto_spawn_registry.values():
            if not template.start_reuse_existing:
                continue
            start = template.start_location
            resident = start.labware or await self._labware_service.get_by_position(start.position_id)
            if resident is not None and resident.id not in in_use and await self._tip_layout_is_unknown(resident):
                raise AcquisitionValidationError(no_stated_tips(resident.name))

    async def _member_facts(
        self, labware: LabwareInstance, template: ThreadTemplate, also_named_by: str | None, held_by: str | None,
    ) -> MemberFacts:
        here = self._system.labware_location_service.present_location(labware)
        jaws = self._system.system_map.find_gripper_location(here.position_id) if here is not None else None
        tip_layout_unknown = await self._tip_layout_is_unknown(labware)
        return MemberFacts(
            labware_name=labware.name,
            labware_type=labware.labware_type,
            wanted_type=template.labware_template.labware_type,
            thread_template=template.name,
            also_named_by=also_named_by,
            held_by=held_by,
            in_jaws_of=jaws.name if jaws is not None else None,
            tip_layout_unknown=tip_layout_unknown,
            start_dispenses=template.start_dispense,
            start=template.start_location.name,
        )
    def _adopt_unused_plates_on_start_locations(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup],
        resolved: dict[tuple[str, str], ResolvedAcquisition],
    ) -> tuple[Sequence[LabwareGroup], set[str]]:
        """Bind an entry thread to the plate an operator put on its start location, in place of making a new one.

        Only a plate of the thread's own template that no run has used, and that neither this submission nor a running
        execution names; anything else stays occupied and refused.
        Returns the groups to run with (a submission with none gets one, because a binding is carried by a group
        member) and the plates bound, which the caller marks used once the submission is accepted.
        """
        names = sorted(t.name for t in workflow.entry_thread_templates)
        members = [(g.id, m.thread_template_name) for g in groups for m in g.members] or [
            (_ADOPTED_GROUP_ID, name) for name in names
        ]
        used = self._plates_used_by_runs | {
            thread.labware.id
            for execution in self._executions.values() if execution.executing_workflow is not None
            for thread in execution.executing_workflow.threads
        }
        named = {r.labware_instance.id for r in resolved.values() if r.labware_instance is not None}
        taken = named | set(self._labware_in_use())
        claimed: set[str] = set()
        for group_id, name in members:
            template = _entry_thread_named(workflow, name)
            if template is None or template.start_reuse_existing or (group_id, name) in resolved:
                continue
            location = template.start_location
            plate = location.labware
            if (
                plate is None or location.is_plate_source or plate.id in used or plate.id in taken
                or plate.id in claimed
                or plate.template_name != template.labware_template.name
            ):
                continue
            resolved[(group_id, name)] = ResolvedAcquisition(labware_instance=plate)
            claimed.add(plate.id)
        if groups or not claimed:
            return groups, claimed
        return [LabwareGroup(id=_ADOPTED_GROUP_ID, members=tuple(LabwareGroupMember(name) for name in names))], claimed

    def _validate_start_locations(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup],
        resolved: dict[tuple[str, str], ResolvedAcquisition],
        join_target: Execution | None = None,
    ) -> None:
        """Refuse the submission if any entry thread's start holds other labware.

        Otherwise the thread would wait at AWAITING_START_LOCATION for a slot
        nothing is going to clear. Runs after `_resolve_acquisitions`. A location
        override is checked in place of the template's start, and a `reuse_existing`
        thread is skipped because it binds what is there. Labware named by this submission,
        or by the run it joins, is not counted if its own thread uses it there, or carries it off to a start
        that will itself be free.
        A finished thread carries nothing off, so its labware is counted.
        """
        occupied: list[OccupiedSlot] = []
        claims = list(resolved.items())
        past_their_starts: frozenset[str] = frozenset()
        if join_target is not None and join_target.executing_workflow is not None:
            past_their_starts = frozenset(
                t.labware.id for t in join_target.executing_workflow.threads
                if t.has_reached_its_start and not LabwareThreadStatus(t.status).is_terminal()
            )
        if join_target is not None:
            finished = {
                t.labware_id for t in self.list_threads(join_target.id)
                if LabwareThreadStatus(t.status).is_terminal()
            }
            claims += [
                (key, claim)
                for submission in join_target.submissions
                for key, claim in submission.resolved_acquisitions.items()
                if claim.labware_instance is None or claim.labware_instance.id not in finished
            ]

        def _record_if_occupied(loc: Location) -> None:
            slot = _occupied_slot(loc)
            if slot is not None:
                occupied.append(slot)

        if not groups:
            for template in workflow.entry_thread_templates:
                if template.start_reuse_existing:
                    continue
                _record_if_occupied(template.start_location)
        else:
            for group in groups:
                for member in group.members:
                    member_template = _entry_thread_named(workflow, member.thread_template_name)
                    if member_template is None or member_template.start_reuse_existing:
                        continue
                    res = resolved.get((group.id, member.thread_template_name))
                    start = (
                        res.start_location
                        if res is not None and res.start_location is not None
                        else member_template.start_location
                    )
                    claimed = res.labware_instance if res is not None else None
                    if start.labware is not None and (
                        start.labware is claimed
                        or _moves_off(workflow, claims, start.labware.id, start, past_their_starts)
                    ):
                        continue
                    _record_if_occupied(start)

        if occupied:
            raise StartLocationsOccupiedError(occupied)

    async def wait_for_execution(self, submission: Submission) -> ExecutionStatus:
        """Block until the submission's execution finishes. Returns the final status snapshot.

        Raises ``SystemStallError`` the moment the stall detector pauses this
        execution: a stalled execution never finishes on its own, so waiters
        must fail fast instead of blocking blind to an external timeout. The
        execution stays PAUSED and operator-recoverable; resume clears the
        episode and a fresh wait can be attached.

        Cancelling this wait (e.g. an asyncio.wait_for timeout) raises: a
        mid-flight snapshot is indistinguishable from a finished run, and
        returning one taught callers to assert on partial state. Use
        ``get_execution`` for a point-in-time snapshot. Execution failures are
        recorded on the execution (if not already set) and logged so they are
        discoverable via the returned status snapshot.
        """
        execution = self._executions[submission.execution_id]
        await self._await_completion_or_stall(execution)
        try:
            execution.task.result()
        except asyncio.CancelledError:
            # The EXECUTION was cancelled (abort/shutdown); the wait itself
            # finished. The snapshot reports the aborted phase.
            logger.info(
                "Execution '%s' (%s) was cancelled; returning final snapshot",
                execution.id,
                execution.workflow_name,
            )
        except Exception as exc:
            if execution.error is None:
                execution.error = str(exc)
            logger.error(
                "Execution '%s' (%s) failed: %s",
                execution.id,
                execution.workflow_name,
                exc,
                exc_info=exc,
            )
        # Drain the call_soon ``_on_task_done`` callback before snapshotting the
        # phase, else a stopped exec reads STOPPING. Same race as ``wait``.
        await asyncio.sleep(0)
        return self._status_snapshot(execution)

    async def _await_completion_or_stall(self, execution: Execution) -> None:
        """Block until the execution task completes, raising ``SystemStallError``
        the moment the stall detector fails this execution's waiters. Leaves the
        task's result unconsumed for the caller."""
        stall_waiter = asyncio.ensure_future(execution.stall_event.wait())
        try:
            done, _ = await asyncio.wait(
                {execution.task, stall_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stall_waiter.cancel()
        if execution.task not in done:
            report = execution.stall_report or StallReport(
                thread_ids=(), waits=("stall detected",),
            )
            raise SystemStallError(execution.id, report)

    def _status_snapshot(self, execution: Execution) -> ExecutionStatus:
        return ExecutionStatus(
            id=execution.id,
            workflow_name=execution.workflow_name,
            status=execution.phase,
            error=execution.error,
            paused=execution.is_paused,
            pause_reason=execution.pause_reason,
            abort_armed=execution.abort_armed,
            simulated=execution.simulated,
            sim_speedup=execution.sim_speedup,
        )

    def _get_execution(self, execution_id: str) -> Execution:
        execution = self._executions.get(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' not found")
        return execution

    def iter_executions(self) -> Iterator[Execution]:
        """Iterate all tracked Execution objects.

        Public alias for internal execution map iteration; used by the
        SubmissionFacade to walk submissions across every execution.
        """
        return iter(self._executions.values())

    def require_execution(self, execution_id: str) -> Execution:
        """Alias for the internal `_get_execution`. Raises KeyError on miss."""
        return self._get_execution(execution_id)

    def get_execution_status(self, execution_id: str) -> ExecutionStatus:
        """Get current status of an execution by ID."""
        return self._status_snapshot(self._get_execution(execution_id))

    def list_executions(self) -> list[ExecutionRecord]:
        """List all tracked executions.

        Returns `ExecutionRecord` (the flat daemon shape) rather than
        `ExecutionStatus`. Daemon routes and DTOs consume ExecutionRecord;
        the richer `ExecutionStatus` is available via `get_execution_status`.
        """
        return [self.get_execution(e.id) for e in self._executions.values()]

    async def wait_for_runs_to_end(self) -> None:
        # Each end is bounded by END_EVENTS_SAVE_TIMEOUT_S, so this wait is too.
        ending = [e for e in self._executions.values() if e.phase in _TERMINAL_PHASES and not e.ended.is_set()]
        await asyncio.gather(*(e.ended.wait() for e in ending))

    def list_reservations(self, execution_id: str) -> list[ReservationSnapshot]:
        """List all active reservations for an execution."""
        execution = self._get_execution(execution_id)
        if execution.executing_workflow is None:
            return []
        raw = execution.executing_workflow.get_active_reservations()
        out: list[ReservationSnapshot] = []
        for loc, rid, tid in raw:
            held = execution.executing_workflow.get_reservation_at(loc)
            labware = held.labware if held is not None else None
            out.append(ReservationSnapshot(
                position_id=loc, reservation_id=rid, thread_id=tid,
                labware_name=labware.name if labware is not None else None,
                awaiting_operator=(
                    held is not None
                    and held.priority is ReservationPriority.AWAITING_OPERATOR
                ),
                arriving=(
                    held is not None
                    and held.priority is ReservationPriority.MOVE_TARGET
                ),
            ))
        return out

    def cancel_reservation(self, execution_id: str, reservation_id: str) -> None:
        """Cancel one active reservation owned by a thread in this execution.

        Four checks (each maps to `KeyError` -> 404 at the route):
        1. Execution must be known to the runtime.
        2. Execution must have an `executing_workflow` (task has started).
        3. Some active reservation across the whole coordinator must have
           this id. The coordinator is system-wide, so this scan is where
           cross-execution ids are visible.
        4. The reservation's owning thread must belong to THIS execution.
           Cross-execution attempts get "not found" treatment so the failure
           does not leak the existence of another execution's reservation.
        """
        execution = self._get_execution(execution_id)
        if execution.executing_workflow is None:
            raise KeyError(f"Reservation '{reservation_id}' not found")
        active = execution.executing_workflow.get_active_reservations()
        match = next((row for row in active if row[1] == reservation_id), None)
        if match is None:
            raise KeyError(f"Reservation '{reservation_id}' not found")
        _, _, rsv_thread_id = match
        exec_thread_ids = {t.id for t in self._get_execution_threads(execution_id)}
        if rsv_thread_id not in exec_thread_ids:
            raise KeyError(f"Reservation '{reservation_id}' not found")
        execution.executing_workflow.cancel_reservation_by_id(reservation_id)

    def list_pending_manual_steps(
        self, execution_id: str | None = None,
    ) -> list[PendingManualStepRecord]:
        """List emitted-but-unconfirmed operator manual steps.

        ``execution_id=None`` spans every tracked execution; a specific id
        scopes to one (KeyError if unknown). Executions whose workflow has
        not started yet (``executing_workflow is None``) contribute nothing.
        """
        if execution_id is None:
            records: list[PendingManualStepRecord] = []
            for execution in self._executions.values():
                if execution.executing_workflow is None:
                    continue
                registry = execution.executing_workflow.event_channel_registry
                records.extend(
                    PendingManualStepRecord(
                        execution_id=execution.id,
                        step_id=step.step_id,
                        instruction=step.instruction,
                        emitted_at=step.emitted_at,
                    )
                    for step in registry.list_manual_steps()
                )
            return records
        execution = self._get_execution(execution_id)
        if execution.executing_workflow is None:
            return []
        registry = execution.executing_workflow.event_channel_registry
        return [
            PendingManualStepRecord(
                execution_id=execution.id,
                step_id=step.step_id,
                instruction=step.instruction,
                emitted_at=step.emitted_at,
            )
            for step in registry.list_manual_steps()
        ]

    async def confirm_manual_step(self, execution_id: str, step_id: str) -> None:
        """Confirm a pending operator manual step, releasing the waiting action.

        Raises KeyError for an unknown execution, a not-yet-started
        execution, or an unknown / already-confirmed / timed-out step_id.
        The claim is a synchronous pop with no await before it, so two
        concurrent confirms cannot both succeed: the first pops the entry,
        the second sees it gone and reports not-found.
        """
        not_found = (
            f"manual step '{step_id}' not found in execution '{execution_id}'; "
            "run 'manual-step list' to see pending steps"
        )
        execution = self._get_execution(execution_id)
        if execution.executing_workflow is None:
            raise KeyError(not_found)
        registry = execution.executing_workflow.event_channel_registry
        if registry.pop_manual_step(step_id) is None:
            raise KeyError(not_found)
        channel = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}")
        await channel.publish(value="confirmed")

    def pause_execution(
        self, execution_id: str, reason: str = "manual", message: str | None = None,
    ) -> dict[str, int]:
        """Pause an execution immediately: set the execution-level latch and
        fan the pause out to every thread.

        The latch (``paused_at``) is orthogonal to ``phase`` -- a paused
        execution stays ACCEPTING/DRAINING. It gates new submissions
        (JOIN_EXISTING is refused at the submit acceptance path).

        Threads spawned after this call are born paused too (see
        ``pause_all_threads``). A thread the operator spawns by hand
        (``spawn_thread_in_execution``) is exempt and still starts running
        while paused, by design.

        The latch is set immediately even though a thread inside a long device
        action only reaches PAUSED at its next safe point. Idempotent:
        re-pausing keeps the original ``paused_at``. Returns the per-thread
        counters from ``pause_all_threads``.

        ``reason`` is "manual" for an operator-initiated pause, "system" when
        the runtime is pausing itself (a stall, an unresolvable deadlock) --
        see ``ExecutingLabwareThread.request_pause``. A recoverable timeout
        does not come through here: it fans the pause out to the threads
        without setting the latch, so an operator's stop still outranks it.
        """
        execution = self._get_execution(execution_id)
        if execution.paused_at is None:
            execution.paused_at = datetime.now(timezone.utc)
        return self.pause_all_threads(execution_id, reason=reason, message=message)

    def _pause_for_an_unsaved_record(self, execution_id: str, message: str) -> bool:
        """Pause a run whose record could not be saved, as the operator's Pause does. False when it is not running here."""
        execution = self._executions.get(execution_id)
        if execution is None or _phase_to_state(execution.phase).is_terminal() or execution.task.done():
            return False
        self.pause_execution(execution_id, reason="system", message=message)
        return True

    def resume_execution(self, execution_id: str) -> dict[str, int]:
        """Resume a paused execution: clear the latch, disarm any armed abort,
        and fan the resume out to every thread.

        Disarming on resume (C4) ensures a stale ``abort_armed`` from an
        earlier stop call plus a later confirm cannot abort a now-running
        execution. Returns the per-thread counters from ``resume_all_threads``.
        """
        execution = self._get_execution(execution_id)
        execution.paused_at = None
        execution.abort_armed = False
        # ONLY the execution-level resume drains quarantine; the recoverable-
        # timeout path calls resume_all_threads directly and must never dispose.
        wf = execution.executing_workflow
        if wf is not None and wf.has_orphaned_slots():
            drain = asyncio.get_running_loop().create_task(
                wf.drain_orphaned_slots()
            )
            self._background_tasks.add(drain)
            drain.add_done_callback(self._on_drain_done)
        return self.resume_all_threads(execution_id)

    def set_execution_sim_speedup(self, execution_id: str, sim_speedup: float) -> float:
        """Change a simulated execution's speed-up. Applies at once: every
        paced step in progress re-plans the rest of its wait at the new speed-up.

        A LIVE or check-run execution has no pacer and refuses; so does a finished one.
        Emits ``EXECUTION.<id>.SIM_SPEEDUP_CHANGED`` so every viewer's control
        follows.
        """
        execution = self._get_execution(execution_id)
        if _phase_to_state(execution.phase).is_terminal():
            raise RuntimeError(
                f"Execution '{execution_id}' is {execution.phase.value}; "
                f"its sim speed-up cannot change"
            )
        if execution.sim_pacer is None:
            raise SimSpeedupNotApplicableError(
                _UNPACED_REASON[execution.submissions[0].run_mode], execution_id=execution_id,
            )
        speedup = validate_sim_speedup(sim_speedup)
        execution.sim_pacer.set_speedup(speedup)
        self._safe_emit(
            f"EXECUTION.{execution.id}.SIM_SPEEDUP_CHANGED",
            ExecutionSimSpeedupContext(
                execution_id=execution.id,
                workflow_name=execution.workflow_name,
                sim_speedup=speedup,
            ),
        )
        return speedup

    def _on_drain_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Accept-partial drain failed", exc_info=task.exception(),
            )

    async def abort_execution(self, execution_id: str) -> None:
        """Abortively stop a running execution: cancel every thread now.

        Abort is hard, not cooperative (``close_execution`` is the cooperative
        drain). Cancelling only ``execution.task`` reaches the entry threads in
        its gather, but dynamically spawned co-threads are detached fire-and-
        forget tasks that would survive -- leaking reservations and leaving
        stuck shared actions for an orphan to re-drive. So this also cancels
        every retained thread task via ``stop_all_thread_tasks`` (dynamic
        seen-loop, so late spawns during the drain are caught too). Each
        cancelled thread's ``start()`` finally releases its reservations,
        which is what lets a subsequent run acquire its start reservation
        instead of wedging at CREATED.

        Phase moves to ``STOPPING`` immediately; ``_on_task_done`` flips it to
        ``ABORTED`` once the task callback fires. Cooperative parking remains
        deferred -- do not read this as a graceful unwind.

        This is the raw abort primitive used by forced teardown (shutdown
        sweep, failed-build cleanup). The operator-facing two-call verb is
        ``stop_execution``, which arms first and calls this only on a
        confirmed second call.

        Idempotent on a task that already-finished-but-not-yet-callbacked
        (``task.done()`` true but ``_on_task_done`` not yet run): yield once
        so the pending callback drains and the phase is the real terminal
        value, not a STOPPING flicker.
        """
        execution = self._get_execution(execution_id)
        if execution.task.done():
            await asyncio.sleep(0)
            return
        # Abort ends any stall episode: an aborted execution is not stalled,
        # and waiters must see the task finish, not the stale stall event.
        self._end_stall_episode(execution_id)
        if execution.phase in (
            ExecutionPhase.ACCEPTING, ExecutionPhase.DRAINING,
        ):
            execution.phase = ExecutionPhase.STOPPING
        if execution.executing_workflow is not None:
            await execution.executing_workflow.stop_all_thread_tasks()
        execution.task.cancel()

    async def stop_execution(
        self, execution_id: str, *, confirm: bool = False,
    ) -> StopOutcome:
        """Operator stop: a two-call confirmed abort (Decision C).

        Call 1 (``confirm=False``), a cold confirmed call, or a confirm that
        arrives while not yet armed: pause immediately + arm. Returns
        ``StopOutcome(armed=True, aborted=False)``; the execution is NOT
        aborted. Two distinct intentful calls are always required, so a single
        stray confirmed call can never abort.

        Call 2 (``confirm=True`` on an armed, still-paused execution): run the
        abort primitive, then drain to the terminal phase so the response
        reports ABORTED, not a transient STOPPING (the pre-existing status
        race, fixed here). Returns ``StopOutcome(aborted=True)``.

        Resume disarms (see ``resume_execution``), so a stale arm plus a later
        confirm cannot abort a resumed execution.
        """
        execution = self._get_execution(execution_id)
        if _phase_to_state(execution.phase).is_terminal():
            return StopOutcome(
                armed=False, aborted=False, phase=execution.phase,
            )
        if confirm and execution.abort_armed and execution.is_paused:
            await self.abort_execution(execution_id)
            await self.wait(execution_id)
            return StopOutcome(
                armed=False, aborted=True, phase=execution.phase,
            )
        self.pause_execution(execution_id)
        execution.abort_armed = True
        return StopOutcome(
            armed=True, aborted=False, phase=execution.phase,
        )

    def remove_execution(self, execution_id: str) -> None:
        """Remove a completed or aborted execution and clear its events.

        The terminal-state guard surfaces as 409 at the route.
        """
        execution = self._get_execution(execution_id)
        state = _phase_to_state(execution.phase)
        if not state.is_terminal():
            raise RuntimeError(
                f"Execution '{execution_id}' is {state.value}; "
                f"can only remove terminal executions "
                f"(completed, partial_completed, failed, aborted)"
            )
        if execution.executing_workflow is not None:
            self._plates_used_by_runs |= {t.labware.id for t in execution.executing_workflow.threads}
        del self._executions[execution_id]

    def get_execution_detail(self, execution_id: str) -> ExecutionDetail:
        """Get detailed status including all thread snapshots."""
        execution = self._get_execution(execution_id)
        execution_threads = self._get_execution_threads(execution_id)
        threads = [_build_thread_snapshot(t) for t in execution_threads]
        completed = sum(1 for t in threads if t.status == "COMPLETED")
        # ABORTED counts as terminal (not active) so a single-thread
        # execution where the operator ABORT_THREAD'd doesn't report a
        # phantom in-flight thread. STOPPING is in-progress
        # rather than terminal -- it's the transient state between
        # request and the actual STOPPED transition.
        active = sum(
            1 for t in threads
            if t.status not in ("COMPLETED", "ABORTED", "STOPPED", "FAILED", "STOPPING")
        )
        return ExecutionDetail(
            id=execution.id,
            workflow_name=execution.workflow_name,
            status=execution.phase,
            error=execution.error,
            threads=threads,
            total_thread_count=len(threads),
            completed_thread_count=completed,
            active_thread_count=active,
            paused=execution.is_paused,
            pause_reason=execution.pause_reason,
            abort_armed=execution.abort_armed,
            simulated=execution.simulated,
            sim_speedup=execution.sim_speedup,
        )

    # -- Blocker sources (see orca.runtime.blockers) ------------------------

    async def blocker_device_snapshots(self) -> list[DeviceSnapshot]:
        """Every device and transporter, with its fault and hold state."""
        return await declared_device_snapshots(self._device_registry, self.devices)

    def blocker_execution_details(self) -> list[ExecutionDetail]:
        """Detail for every execution that has not finished."""
        return [
            self.get_execution_detail(execution.id)
            for execution in self._executions.values()
            if execution.phase not in _TERMINAL_PHASES
        ]

    def blocker_reservations(self) -> list[ReservationSnapshot]:
        """Active reservations across every live execution, each tagged."""
        out: list[ReservationSnapshot] = []
        for execution in self._executions.values():
            if execution.phase in _TERMINAL_PHASES:
                continue
            out.extend(
                replace(row, execution_id=execution.id)
                for row in self.list_reservations(execution.id)
            )
        return out

    def blocker_occupants(self) -> dict[str, LabwareOccupant]:
        return {
            location.name: LabwareOccupant(labware_id=location.labware.id, labware_name=location.labware.name)
            for location in self._system.locations
            if location.labware is not None
        }

    def blocker_pending_manual_steps(self) -> list[PendingManualStepRecord]:
        return self.list_pending_manual_steps()

    async def blocker_open_incidents(self) -> list[SystemIncident]:
        return await self.incidents.list(unacknowledged_only=True)

    async def blockers(self) -> list[Blocker]:
        """Everything stopping the run right now, worst first."""
        return await derive_blockers(self)

    def list_threads(self, execution_id: str) -> list[ThreadSnapshot]:
        """List all thread snapshots for an execution."""
        return [_build_thread_snapshot(t) for t in self._get_execution_threads(execution_id)]

    def get_thread_detail(self, execution_id: str, thread_id: str) -> ThreadSnapshot:
        """Get a single thread snapshot by ID."""
        for thread in self._get_execution_threads(execution_id):
            if thread.id == thread_id:
                return _build_thread_snapshot(thread)
        raise KeyError(f"Thread '{thread_id}' not found in execution '{execution_id}'")

    def get_paused_threads(self, execution_id: str) -> list[ThreadSnapshot]:
        """List threads in PAUSED state for a given execution."""
        return [t for t in self.list_threads(execution_id) if t.status == "PAUSED"]

    def recover_thread(self, execution_id: str, thread_id: str, decision: RecoveryDecision) -> None:
        """Resume a paused thread with the given recovery decision."""
        thread = self._find_thread(execution_id, thread_id)
        thread.resume_with_decision(decision)

    def fault_named_by_pause(
        self, execution_id: str, thread_id: str,
    ) -> DeviceFault | None:
        """The device fault this thread's error pause is about, or None."""
        return _fault_named_by_pause(self._find_thread(execution_id, thread_id))

    async def clear_fault_if_current(self, fault: DeviceFault) -> None:
        """Clear this fault, and only this one.

        A device carries one fault at a time and the first is the one kept, so
        a different one standing means the pause was about trouble already
        dealt with. Clearing by device name alone would drop a fault nobody has
        looked at yet.
        """
        if self._device_controller.fault(fault.device_id) is fault:
            await self._device_controller.clear_fault(fault.device_id)

    def pause_thread(self, execution_id: str, thread_id: str) -> None:
        """Request a thread to pause at its next safe point (between actions)."""
        thread = self._find_thread(execution_id, thread_id)
        thread.request_pause()

    def resume_thread(self, execution_id: str, thread_id: str) -> None:
        """Resume a manually paused thread, or cancel a pending pause.

        If the thread is currently PAUSED, resumes via the manual-pause
        path. If a ``request_pause`` was queued but had not yet fired
        (thread is still MOVING / EXECUTING_ACTION / AWAITING_CO_THREADS
        when resume arrives), cancels the queued pause so the thread
        keeps running instead of latching into PAUSED later. This
        mirrors the operator expectation that pause + resume in
        sequence is a no-op even when pause hadn't yet reached a safe
        point.

        Raises ``ValueError`` for error-paused threads (use
        ``recover_thread`` with an explicit decision).
        """
        thread = self._find_thread(execution_id, thread_id)
        if thread.status == LabwareThreadStatus.PAUSED:
            thread.resume_from_manual_pause()
        else:
            thread.cancel_pending_pause()
        workflow = self._get_execution(execution_id).executing_workflow
        if workflow is not None:
            workflow.retry_the_unserved()

    def pause_all_threads(
        self, execution_id: str, reason: str = "manual", message: str | None = None,
    ) -> dict[str, int]:
        """Request all threads in an execution to pause at their next safe point.

        Returns three counters so the CLI / REST caller can tell the operator
        whether anything actually pauseable was found:

        * ``pausing``           -- threads asked to pause (running threads
          that will pause at their next safe point).
        * ``already_paused``    -- threads already paused (no-op).
        * ``terminal_skipped``  -- threads that have already terminated
          (COMPLETED, STOPPED) and cannot be paused.

        Scoped to the threads of the given execution via
        ``_get_execution_threads`` rather than the system-global
        ``executing_threads`` registry; otherwise the counters inflate
        with peer-execution threads when more than one workflow is in
        flight.

        New threads are held before the fan-out, so a thread spawned after this
        call starts paused and moves no labware on a run the operator stopped.
        A parent blocked on a reservation when the pause lands pauses where it
        waits and spawns nothing. The hold is recorded on the
        execution even when the workflow has not attached yet, because a stop
        can land between submit and boot; the attach applies it to the entry
        threads. The counters cover the fan-out only, since a held thread that
        has not started is not in the list.

        ``reason`` and ``message`` forward to each thread's ``request_pause``:
        "manual" (no message) for an operator, "system" plus the runtime's own
        cause (a stall report, an incident) when it pauses on its own behalf.
        """
        execution = self._get_execution(execution_id)
        execution.new_threads_held = True
        execution.new_threads_hold_reason = reason
        if execution.executing_workflow is not None:
            execution.executing_workflow.hold_new_threads(reason=reason)
        threads = self._get_execution_threads(execution_id)
        pausing = 0
        already_paused = 0
        terminal_skipped = 0
        for thread in threads:
            if thread.status.is_terminal():
                terminal_skipped += 1
                continue
            if thread.status == LabwareThreadStatus.PAUSED:
                already_paused += 1
                continue
            thread.request_pause(reason=reason, message=message)
            pausing += 1
        return {
            "pausing": pausing,
            "already_paused": already_paused,
            "terminal_skipped": terminal_skipped,
        }

    async def no_answer(
        self, device_name: str, command: str, command_id: str, execution_id: str | None,
        seconds: float, why: NoAnswer, statement: CommandStatement | None, named: Sequence[str],
    ) -> str | None:
        """A command got no answer, was stopped mid-way, or failed after the device took it: what it did
        is unknown.

        The labware it names goes unobserved, or everything standing at the device when it names none, and so
        does the head when it is a tip command. One incident asks someone to look. The incident keeps what the
        command states, so abort or mark complete settles the record even after a restart. A run's timeout already filed its incident and
        held the run; aborting it files no second.
        """
        moved = [] if statement is None or statement.motion is None else [statement.motion.labware.name]
        await self._system.note_no_answer(device_name, execution_id, [*named, *moved], command)
        if why is NoAnswer.TIMEOUT_ABORTED:
            return None
        if why is NoAnswer.FAILED:
            statement = stated_after_a_failure(statement)
        incident = self._incident_service.record(
            category=IncidentCategory.RECOVERABLE_TIMEOUT,
            severity=IncidentSeverity.WARNING,
            message=_outcome_unknown_message(device_name, command, seconds, why),
            detail=RecoverableTimeoutContext(
                device_id=device_name, command=command, command_id=command_id,
                elapsed_seconds=seconds, max_seconds=seconds if why is NoAnswer.TIMED_OUT else 0.0,
                unheld=stored_command(device_name, statement),
            ),
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
        )
        return incident.id

    def declare_recoverable_timeout(
        self,
        execution_id: str,
        context: RecoverableTimeoutContext,
    ) -> SystemIncident:
        """Record a RECOVERABLE_TIMEOUT incident and pause every thread in the execution.

        The device-command dispatcher invokes this when an in-flight
        device command exceeds its ``max_seconds`` without a response.
        Unlike ``declare_unresolvable_deadlock``, the engine itself
        never raises this -- it always comes from the dispatcher.

        On invocation:
          1. Records one incident under
             ``IncidentCategory.RECOVERABLE_TIMEOUT`` with the device,
             command, command_id, elapsed seconds, and the max that was
             exceeded. Surfaces on every operator surface that consumes
             the IncidentService: ``orca incidents list`` (CLI),
             ``GET /api/incidents`` (REST).
          2. Fans out ``pause_all_threads(execution_id)`` so every thread
             in the execution pauses at its next safe point. The
             workflow thread that submitted the overrun command is
             already blocked on the held future; pausing siblings
             prevents them from racing ahead while the operator
             deliberates.

        The operator resolves the incident through ``incidents.``
        ``recoverable_timeout_extend`` / ``_abort`` / ``_mark_complete``
        (exposed over REST / CLI). The coordinator settles the held
        call's eventual state and calls ``resume_all_threads(execution_id)``
        once the workflow is safe to continue; abort and mark_complete also
        emit a wire cancel so the transport discards a late response.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        incident = self._incident_service.record(
            category=IncidentCategory.RECOVERABLE_TIMEOUT,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Recoverable timeout: command '{context.command}' on "
                f"device '{context.device_id}' has been running for "
                f"{context.elapsed_seconds:.1f}s (max "
                f"{context.max_seconds:.1f}s). Execution paused; "
                f"operator may extend, abort, or mark complete."
            ),
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
        )
        # Recording the incident is the contract; pausing siblings is
        # best-effort. If the execution has already been removed (a force-stop
        # racing the timeout), still surface the incident rather than 500.
        try:
            self.pause_all_threads(execution_id, reason="system", message=incident.message)
        except KeyError:
            logger.warning(
                "recoverable timeout for unknown execution %s; incident recorded, "
                "no threads to pause", execution_id,
            )
        return incident

    def resume_all_threads(self, execution_id: str) -> dict[str, int]:
        """Resume every manually-paused thread + cancel pending pauses.

        Returns four counters so the CLI / REST caller can tell the
        operator exactly what happened:

        * ``resumed``               -- threads that were PAUSED manually and are now running.
        * ``pause_cancelled``       -- threads that had a pending ``request_pause`` queued
          (from an earlier ``pause_all_threads``) but had not yet
          reached PAUSED; their pending pause is cancelled so they
          continue running instead of latching into PAUSED later.
          This closes a footgun where pause + resume in quick
          succession would leave threads pausing seconds afterward
          because pause was queued but hadn't fired at resume time.
        * ``error_skipped``         -- threads that were PAUSED with an error (need explicit `recover`).
        * ``completed_skipped``     -- threads that terminated between the status check and the resume call.

        Scoped to the threads of the given execution (parallel to
        ``pause_all_threads``); otherwise multi-execution systems
        misattribute peer threads to this execution's counters.

        Refuses outright while the execution-level pause latch is set, and
        returns all-zero counters. The recoverable-timeout coordinator calls
        this directly to let a run continue once an operator settles a
        timed-out device call; an operator stop outranks that, so a run the
        operator stopped stays stopped and its held threads stay held. Only
        ``resume_execution``, which clears the latch first, resumes a stopped
        run.
        """
        # Resume ends the stall episode: clear the waiter event and re-arm the
        # detector so a persisting wedge reports (and re-fails waiters) afresh.
        self._end_stall_episode(execution_id)
        execution = self._get_execution(execution_id)
        if execution.is_paused:
            return {
                "resumed": 0, "pause_cancelled": 0,
                "error_skipped": 0, "completed_skipped": 0,
            }
        execution.new_threads_held = False
        execution.new_threads_hold_reason = "manual"
        if execution.executing_workflow is not None:
            execution.executing_workflow.release_new_threads()
            execution.executing_workflow.retry_the_unserved()
        threads = self._get_execution_threads(execution_id)
        resumed = 0
        pause_cancelled = 0
        error_skipped = 0
        completed_skipped = 0
        for thread in threads:
            if thread.status.is_terminal():
                completed_skipped += 1
                continue
            if thread.status == LabwareThreadStatus.PAUSED:
                if thread.last_error is not None:
                    logger.warning(
                        "Skipping error-paused thread %s (error: %s)",
                        thread.name, thread.last_error
                    )
                    error_skipped += 1
                else:
                    thread.resume_from_manual_pause()
                    resumed += 1
                continue
            # Non-terminal, non-paused: cancel any queued pause request
            # so threads which had pause queued but hadn't yet reached a
            # safe point keep running instead of pausing later.
            if thread.cancel_pending_pause():
                pause_cancelled += 1
        return {
            "resumed": resumed,
            "pause_cancelled": pause_cancelled,
            "error_skipped": error_skipped,
            "completed_skipped": completed_skipped,
        }

    def acknowledge_incident(self, incident_id: str) -> bool:
        """Enqueue an acknowledgement (fire-and-forget; the durable write does
        not validate). Always returns True. Unknown-id rejection is the
        operator-facing facade's job: IncidentFacade.acknowledge gates on a real
        read and raises KeyError. The only internal caller (the recoverable-
        timeout coordinator) acks an id it just held, so it cannot be unknown."""
        self._incident_service.acknowledge(incident_id)
        return True

    async def clear_device_fault(self, device_id: str) -> None:
        """Drop a fault on the operator's word that the device finished.

        Called by the recoverable-timeout coordinator on mark-complete, where
        the cancel that resolves the timeout is what latched the fault.
        """
        await self._device_controller.clear_fault(device_id)

    @property
    def incident_declarer(self) -> IncidentDeclarer:
        return self._incident_declarer

    @property
    def recoverable_timeout_coordinator(self) -> RecoverableTimeoutCoordinator:
        """The engine's recoverable-timeout coordinator.

        Seeded onto the per-thread ``recoverable_timeout_coordinator`` ContextVar
        at thread start so device dispatch can park a timed-out call here.
        """
        return self._recoverable_timeouts

    def sim_pacer_for(self, execution_id: str) -> SimPacer | None:
        execution = self._executions.get(execution_id)
        return None if execution is None else execution.sim_pacer

    async def spawn_thread_in_execution(
        self,
        execution_id: str,
        template_name: str,
        labware_id: str | None = None,
    ) -> ThreadSnapshot:
        """Manually create and start a thread in a live execution.

        The operator names the template and optionally pins an existing labware
        instance. The thread runs on its own: it does not join an action another
        thread is waiting at, so it does not recover AUTO_SPAWN_FAILED.
        """
        execution = self._get_execution(execution_id)
        if execution.executing_workflow is None:
            raise RuntimeError(
                f"Execution '{execution_id}' has not attached a workflow yet; "
                f"cannot spawn threads until the workflow is running."
            )
        if execution.task.done():
            raise RuntimeError(
                f"Execution '{execution_id}' is {execution.phase.value}; "
                f"cannot spawn new threads into a terminal execution."
            )
        template = self._find_thread_template(execution.workflow_name, template_name)
        if template is None:
            known = sorted(
                name
                for (wf_name, name) in self._system.get_labware_thread_templates().keys()
                if wf_name == execution.workflow_name
            )
            raise ThreadTemplateNotFoundError(
                f"No thread template named '{template_name}' in workflow "
                f"'{execution.workflow_name}'. Known: {known}"
            )
        labware_instance = None
        if labware_id is not None:
            found = await self._labware_service.get_by_id(labware_id)
            if found is None:
                raise LabwareNotFoundError(labware_id)
            if found.template is not template.labware_template:
                raise ValueError(
                    f"Labware '{labware_id}' has template "
                    f"'{found.template.name if found.template else None}', "
                    f"incompatible with thread template '{template.name}' "
                    f"which expects labware of type "
                    f"'{template.labware_template.name}'."
                )
            labware_instance = found
        # Operator-spawned threads run in whatever mode the execution was
        # accepted under. The one-execution-one-run_mode invariant
        # guarantees every submission in the execution shares the same
        # run_mode, so submissions[0] is canonical. Submission existence
        # is guaranteed transitively by the `executing_workflow is None`
        # guard at line 1306: by the time `executing_workflow` is set
        # `_run_workflow` has already run past its own
        # `submissions[0]` read (system_runtime.py:1861), so the
        # submission list is non-empty here.
        # Never-rebind extends to operator injection: a same-labware receiver
        # spawned into quarantine would consume methods bound to the dead thread.
        wf = execution.executing_workflow
        if template.labware_template.name in wf.orphaned_labware_template_names():
            raise RuntimeError(
                f"Labware template '{template.labware_template.name}' has an "
                f"orphaned backlog in execution '{execution_id}'; resume the "
                f"execution to accept the partial fill before spawning a "
                f"replacement thread."
            )
        op_run_mode: WorkflowRunMode = execution.submissions[0].run_mode
        executing_thread = await execution.executing_workflow.add_and_start_thread(
            template, labware_instance=labware_instance, run_mode=op_run_mode,
        )
        return _build_thread_snapshot(executing_thread)

    def mutate_on_next_pause(
        self,
        execution_id: str,
        thread_id: str,
        callback: Callable[[ISystem, str], None],
    ) -> None:
        """Request pause, execute callback when PAUSED, then resume.

        If the thread is already PAUSED, executes immediately.
        If the thread is COMPLETED/STOPPED, raises ValueError.
        Otherwise, requests cooperative pause and subscribes to THREAD.PAUSED.
        """
        thread = self._find_thread(execution_id, thread_id)
        status = thread.status

        if status.is_terminal():
            raise ValueError(
                f"Thread {thread.name} has status {status.name}, cannot mutate"
            )

        if status == LabwareThreadStatus.PAUSED:
            callback(self._system, thread_id)
            if thread.is_manual_paused:
                self._system.resume_thread(thread_id)
            return

        class _PauseHandler(IEventHandler):
            def __init__(self, system: ISystem, event_bus: IEventBus,
                         tid: str, cb: Callable[[ISystem, str], None],
                         thread_finder: Callable[[str], ExecutingLabwareThread]) -> None:
                self._system = system
                self._event_bus = event_bus
                self._tid = tid
                self._cb = cb
                self._thread_finder = thread_finder
                self._fired = False

            def set_system(self, system: ISystem) -> None:
                pass

            def _unsubscribe_all(self) -> None:
                self._event_bus.unsubscribe(f"THREAD.{self._tid}.PAUSED", self)
                self._event_bus.unsubscribe(f"THREAD.{self._tid}.COMPLETED", self)

            def handle(self, event: str, context: ExecutionContext) -> None:
                if self._fired:
                    return
                if not isinstance(context, ThreadExecutionContext):
                    return
                if context.thread_id != self._tid:
                    return
                self._fired = True
                self._unsubscribe_all()

                if "COMPLETED" in event:
                    logger.warning(
                        "mutate_on_next_pause: thread %s completed before reaching "
                        "pause checkpoint. Mutation callback was not executed.",
                        self._tid,
                    )
                    return

                self._cb(self._system, self._tid)
                # Only auto-resume if manually paused (not error-paused).
                # Error-paused threads need operator recovery via recover_thread.
                thread = self._thread_finder(self._tid)
                if thread.is_manual_paused:
                    self._system.resume_thread(self._tid)

        handler = _PauseHandler(
            self._system, self._event_bus, thread_id, callback,
            lambda tid: self._find_thread(execution_id, tid),
        )
        self._event_bus.subscribe(f"THREAD.{thread_id}.PAUSED", handler)
        self._event_bus.subscribe(f"THREAD.{thread_id}.COMPLETED", handler)
        self._system.pause_thread(thread_id)

    async def _refuse_unless_its_devices_are_ready(self, mode: WorkflowRunMode) -> None:
        """Refuse a fresh run at its start, never midway, when a device it uses cannot run.

        Every run brings its devices up here, after the cheap refusals and before it is accepted; a check run's
        orca-client runs inside Orca. A device that does not come up, or whose orca-client is not connected,
        refuses the run unless it is in a pool where another device came up. Each one records an incident; a
        tolerated one as a warning.
        """
        await self._assert_deck_layouts_configured(mode)
        await self.flush_deck_reseeds()
        unreachable = await self._device_registry.unreachable(
            e.name for e in self._topology_registry.list_devices()
        )
        with mode_scope(mode):
            if not any(not execution.task.done() for execution in self._executions.values()):
                # A run already going uses these worlds; rebuilding them under it would race its records.
                await self._system.device_worlds.mark_current_worlds()
            try:
                tolerated = await self._system.bring_up_for_a_run(unreachable)
            except DevicesNotReadyError as exc:
                for failure in exc.failures:
                    self._incident_declarer.declare_device_init_failure(None, failure)
                raise
        for failure in tolerated:
            self._incident_declarer.declare_device_init_failure(None, failure, tolerated=True)

    async def _refuse_a_join_unless_its_devices_are_reachable(self, mode: WorkflowRunMode) -> None:
        """A join is refused at its start, like a fresh run, when a device the run uses lost its orca-client."""
        if mode is WorkflowRunMode.CHECK_RUN:
            return
        unreachable = await self._device_registry.unreachable(
            e.name for e in self._topology_registry.list_devices()
        )
        try:
            self._system.refuse_a_join_unless_its_devices_are_reachable(unreachable)
        except DevicesNotReadyError as exc:
            for failure in exc.failures:
                self._incident_declarer.declare_device_init_failure(None, failure)
            raise

    async def _assert_deck_layouts_configured(self, mode: WorkflowRunMode) -> None:
        """Reject submissions whose liquid handlers lack a resolvable deck_layout.

        Every ``LiquidHandler`` must declare a ``deck_layout`` that resolves in its ``deck_layout_store``.
        Every offending handler is named in one ``DeckLayoutRequiredError``.
        """
        missing_declaration: list[str] = []
        unresolved_layout: list[tuple[str, str]] = []
        for device in self._liquid_handlers():
            declared = device.deck_layout
            if declared is None:
                missing_declaration.append(device.name)
                continue
            resolved = await device.deck_layout_store.get(declared)
            if resolved is None:
                unresolved_layout.append((device.name, declared))
        if missing_declaration or unresolved_layout:
            raise DeckLayoutRequiredError(
                mode=mode,
                missing_declaration=missing_declaration,
                unresolved_layout=unresolved_layout,
            )

    # -- Flat ExecutionRecord API ---------------------------------------------
    # submit_workflow/wait/get_execution answer with ExecutionRecord; callers that
    # want the Submission model call submit/wait_for_execution/get_execution_status.

    async def submit_workflow(
        self,
        workflow_name: str,
        variables: Mapping[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        sim_speedup: float | None = None,
    ) -> ExecutionRecord:
        """Submit a workflow by name, get back a flat ExecutionRecord.

        If ``deployment_profile`` names a profile registered in
        ``runtime.profile_store``, the runtime resolves it and applies its
        values via ``variable_store.load_profile`` once at submit time.
        Editing the profile via the registry afterwards does NOT affect
        the running execution; the variable store is the only resolver
        after submit.

        ``mode`` is REQUIRED at submit time. No deployment fallback. The run's devices are checked, and for a
        sim or live run brought up, before it is accepted; see ``_refuse_unless_its_devices_are_ready``.
        """
        template = self._system.get_workflow_template(workflow_name)
        return await self._submit_template(
            template, variables, deployment_profile,
            mode=mode,
            sim_speedup=sim_speedup,
        )

    async def _submit_template(
        self,
        template: WorkflowTemplate,
        variables: Mapping[str, OptionValue] | None,
        deployment_profile: str | None,
        *,
        mode: WorkflowRunMode | None,
        sim_speedup: float | None = None,
        every_variable_is_read: bool = True,
    ) -> ExecutionRecord:
        """Shared submit tail for submit_workflow and submit_method.

        The two differ only in how they obtain ``template`` (registry
        lookup vs a synthesized one-method workflow); mode validation,
        submission, variable-store wiring, profile load, and record
        construction are identical.
        """
        if mode is None:
            raise RunModeRequiredError()
        # A shape refusal comes before the device gate: 422 before 409.
        _check_sim_speedup_applies(mode, sim_speedup)
        submission = await self.submit(
            template,
            variables=variables or {},
            deployment_profile=deployment_profile,
            mode=mode,
            sim_speedup=sim_speedup,
            every_variable_is_read=every_variable_is_read,
        )
        execution = self._executions[submission.execution_id]
        return ExecutionRecord(
            id=submission.execution_id,
            workflow_name=submission.workflow_name,
            status=_phase_to_state(execution.phase),
            error=execution.error,
            simulated=execution.simulated,
            sim_speedup=execution.sim_speedup,
        )

    def _named_location(self, name: str) -> Location:
        try:
            return self._system.get_location(name)
        except KeyError as exc:
            raise UnknownLocationError(name) from exc

    async def submit_method(
        self,
        workflow_name: str,
        method_name: str,
        labware_start: Mapping[str, str],
        labware_end: Mapping[str, str],
        variables: Mapping[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        event_hooks: Sequence[str] | None = None,
        sim_speedup: float | None = None,
    ) -> ExecutionRecord:
        """Run one method standalone, synthesizing a one-method workflow.

        Methods do not have their own submission path; they live in some
        registered workflow's bundle. This call resolves the method by
        ``(workflow_name, method_name)``, builds a synthetic
        single-method workflow with the operator-supplied labware start/end
        mappings, and submits it through the regular workflow submission
        machinery so all the standard execution lifecycle (events, audit,
        opshistory, mode validation) applies uniformly.

        Args:
            workflow_name: Registered workflow that owns the method.
            method_name: Method template name within that workflow's bundle.
            labware_start: Mapping of labware-template-name to start
                location-name. Every labware template the method requires
                must be keyed.
            labware_end: Symmetric mapping of labware-template-name to end
                location-name.
            variables, deployment_profile, mode, sim_speedup: Same semantics as
                ``submit_workflow``.
            event_hooks: Which of the workflow's ``wf.on`` handlers the run
                fires, by event name. None fires them all; ``[]`` fires none.

        Raises:
            KeyError: workflow_name unknown.
            MethodNotFoundError: method_name not in that workflow's bundle.
            LabwareTemplateNotFoundError: a labware template name doesn't resolve.
            UnknownLocationError: a start or end location doesn't resolve.
            ValueError: labware_start and labware_end key different labware.
            TypeError: method_name names something that is not a method.
            EventHooksNotInTheWorkflowError: event_hooks names an event the
                workflow has no handler for.
            NoLabwareCanOwnTheMethodError: every labware named is one the
                workflow reuses where it stands.
        """
        parent_workflow = self._system.get_workflow_template(workflow_name)
        method_template = next(
            (m for m in parent_workflow.bundled_methods if m.name == method_name),
            None,
        )
        if method_template is None:
            available = sorted(m.name for m in parent_workflow.bundled_methods)
            raise MethodNotFoundError(
                f"Method '{method_name}' not found in workflow "
                f"'{workflow_name}' bundle. Available: {available}"
            )
        if not isinstance(method_template, MethodTemplate):
            raise TypeError(
                f"bundled method '{method_name}' is not a MethodTemplate "
                f"(got {type(method_template).__name__})"
            )

        hooks = _the_hooks_to_carry(parent_workflow, event_hooks)
        if not labware_start:
            raise ValueError(f"the run of '{workflow_name}.{method_name}' names no labware to start from")

        # Start/end must map the same labware; an unmatched key would hang on
        # co-labware that never arrives (action inputs aren't statically known).
        if set(labware_start) != set(labware_end):
            only_start = sorted(set(labware_start) - set(labware_end))
            only_end = sorted(set(labware_end) - set(labware_start))
            raise ValueError(
                "labware_start and labware_end must map the same labware; "
                f"start-only={only_start}, end-only={only_end}"
            )

        labware_template_index = {
            t.name: t for t in self._system.labware_templates
        }
        threads: list[StandaloneThreadSpec] = []
        for lw_name, start_loc_name in labware_start.items():
            if lw_name not in labware_template_index:
                raise LabwareTemplateNotFoundError(
                    f"Labware template '{lw_name}' not found. Available: "
                    f"{sorted(labware_template_index)}"
                )
            threads.append(with_the_workflows_kinds(
                parent_workflow,
                labware_template_index[lw_name],
                self._named_location(start_loc_name),
                self._named_location(labware_end[lw_name]),
            ))
        owner = next((spec for spec in threads if not starts_from_reuse(spec)), None)
        if owner is None:
            raise NoLabwareCanOwnTheMethodError(method_name, [spec[0].name for spec in threads])
        threads = [owner, *(spec for spec in threads if spec is not owner)]

        # Only the first thread is an entry thread; the rest are auto-spawned,
        # so _validate_start_locations would never look at them and an occupied
        # slot for thread 2..N would spin in the spawn retry until the stall
        # detector fired. Every start location this call routes to is its own.
        # A reused start is meant to be occupied: the plate already standing there is the one the run takes.
        starts = [start for _, start, _ in threads if isinstance(start, Location)]
        occupied = [
            slot for slot in (_occupied_slot(start) for start in starts)
            if slot is not None
        ]
        if occupied:
            raise StartLocationsOccupiedError(occupied)

        # Synthetic workflow name format: prefix identifies it as a
        # standalone-method run; the UUID suffix avoids active-execution
        # collisions when the same method is run multiple times. Operators
        # reading /api/executions can identify these by the prefix and
        # parse out (workflow, method) from the readable suffix.
        synthetic_name = (
            f"standalone-method:{workflow_name}.{method_name}"
            f":{uuid.uuid4().hex[:8]}"
        )
        synthetic = build_standalone_method_workflow(
            synthetic_name, method_template, threads,
            hooks,
        )

        # Carry the parent's variable definitions so method bodies relying on
        # workflow-scoped defaults resolve and explicit values type-validate.
        if parent_workflow.variable_definitions:
            self._system.variable_store.register_workflow_definitions(
                synthetic_name, parent_workflow.variable_definitions,
            )

        return await self._submit_template(
            synthetic, variables, deployment_profile,
            mode=mode, sim_speedup=sim_speedup, every_variable_is_read=False,
        )

    def get_execution(self, execution_id: str) -> ExecutionRecord:
        """Fetch one execution's flat ExecutionRecord."""
        status = self.get_execution_status(execution_id)
        return ExecutionRecord(
            id=status.id,
            workflow_name=status.workflow_name,
            status=_phase_to_state(status.status),
            error=status.error,
            paused=status.paused,
            pause_reason=status.pause_reason,
            abort_armed=status.abort_armed,
            simulated=status.simulated,
            sim_speedup=status.sim_speedup,
        )

    async def wait(self, execution_id: str) -> ExecutionRecord:
        """Block until this execution completes. Returns final ExecutionRecord.

        Raises ``SystemStallError`` when the stall detector pauses this
        execution, same as ``wait_for_execution``: a stalled execution never
        finishes on its own, so blocking blind would hide the wedge.
        """
        if execution_id not in self._executions:
            raise KeyError(f"Execution '{execution_id}' not found")
        execution = self._executions[execution_id]
        await self._await_completion_or_stall(execution)
        try:
            execution.task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        # The run's end is recorded once its events are saved; that wait is bounded.
        await execution.ended.wait()
        return self.get_execution(execution_id)

    # -- Plugin lifecycle --------------------------------------------------

    def unregister_plugin(self, plugin_type: type[OrcaPlugin]) -> None:
        """Unregister all plugins of the given type and drop their event subscriptions."""
        remaining: list[OrcaPlugin] = []
        for p in self._plugins:
            if isinstance(p, plugin_type):
                listener = self._plugin_listeners.pop(id(p), None)
                if listener is not None:
                    self._system_event_bus.unsubscribe(listener)
            else:
                remaining.append(p)
        self._plugins = remaining

    def list_plugins(self) -> list[PluginSnapshot]:
        return [PluginSnapshot(type_name=type(p).__name__) for p in self._plugins]

    def _get_execution_threads(self, execution_id: str) -> list[ExecutingLabwareThread]:
        """Get threads belonging to a specific execution."""
        entry = self._get_execution(execution_id)
        if entry.executing_workflow is not None:
            return entry.executing_workflow.threads
        return []

    def _find_thread_template(self, workflow_name: str, template_name: str) -> ThreadTemplate | None:
        templates = self._system.get_labware_thread_templates()
        return thread_named((t for (wf_name, _), t in templates.items() if wf_name == workflow_name), template_name)

    def _find_thread(self, execution_id: str, thread_id: str) -> ExecutingLabwareThread:
        """Look up an ExecutingLabwareThread by execution and thread ID."""
        for thread in self._get_execution_threads(execution_id):
            if thread.id == thread_id:
                return thread
        raise KeyError(f"Thread '{thread_id}' not found in execution '{execution_id}'")

    async def _run_workflow(self, execution_id: str, workflow: WorkflowTemplate) -> None:
        """Background coroutine: build workflow instance and execute.

        Captures workflow_instance_id for the event forwarder mapping
        before execution starts. Per-group
        entry threads are spawned (one per group per entry template) and
        tagged with group_id + submission_id for group-aware slot keying.

        Every execution is created by `submit()`, which appends the boot
        submission before the task scheduled here is allowed to run, so
        `execution.submissions[0]` is always present. The submission's
        `run_mode` seeds `current_run_mode` for this task; child thread
        tasks inherit it via asyncio context propagation. A sim or live run's
        devices were brought up at submit, so `ensure_runtime_initialized`
        here only does that walk for a check run, or for a device dropped
        since (LH deck configure + fresh device-world init).
        """
        execution = self._executions[execution_id]
        # Boot submission is guaranteed to be present under v3.4: `submit()`
        # appends the submission before the task created in
        # `_build_fresh_execution` gets a chance to yield to this coroutine.
        submission = execution.submissions[0]
        submission_id = submission.id
        groups = submission.groups
        batch_mode = submission.batch_mode
        resolved_acquisitions = submission.resolved_acquisitions
        run_mode = submission.run_mode

        # Seed `current_run_mode` so the lazy-init walk + workflow-instance
        # construction observe the submission's dispatch mode. Child thread
        # tasks inherit this via asyncio task context propagation;
        # `ExecutingLabwareThread.start()` re-seeds with the same value (or
        # a different one for operator-spawned threads).
        current_run_mode.set(run_mode)
        maybe_enable_sim_coroutine_diagnostics_from_env()
        try:
            await self.flush_deck_reseeds()
            await self._system.ensure_runtime_initialized()
        except DevicesNotReadyError as exc:
            for failure in exc.failures:
                self._incident_declarer.declare_device_init_failure(execution_id, failure)
            raise

        # Pass id=execution_id so workflow_instance.id equals execution_id.
        # Unified-id model: one UUID per execution on the submission path,
        # letting the event forwarder stay a set-membership check.
        workflow_instance = await self._system.create_and_register_workflow_instance(
            workflow, submission_id=submission_id, groups=groups,
            batch_mode=batch_mode,
            resolved_acquisitions=resolved_acquisitions,
            id=execution_id,
            run_mode=run_mode,
        )

        self._system.variable_store.create_execution(workflow_instance.id, workflow.name)
        if submission.variables:
            # Write to the submission partition so sibling submissions in a
            # multi-submission execution don't clobber one another's overrides.
            for var_name, value in submission.variables.items():
                self._system.variable_store.set_submission(
                    var_name, value, workflow_instance.id, submission.id,
                )

        self._system.add_workflow(workflow_instance)
        executing_workflow = self._system.get_executing_workflow(workflow_instance.id)
        self._executing_workflows.append(executing_workflow)

        execution.executing_workflow = executing_workflow
        if execution.new_threads_held:
            executing_workflow.hold_new_threads(reason=execution.new_threads_hold_reason)
        execution.workflow_attached.set()
        executing_workflow.set_incident_declarer(self._incident_declarer)

        # SUBMISSION.ACCEPTED for the boot submission fires now that the
        # workflow event bus + forwarder mapping is wired.
        self._accept_submission(workflow_instance.id, workflow.name, submission)

        # Held out here so the finally can reach it: nothing else owns this
        # task between its creation and the await below.
        start_task: asyncio.Task[None] | None = None
        try:
            # Schedule entry threads as a background task so the IN_PROGRESS
            # transition fires when work BEGINS rather than when entry threads
            # complete. ExecutingWorkflow.start() awaits gather() over every
            # entry thread, so awaiting it here would hold ACCEPTED until they end.
            start_task = asyncio.create_task(executing_workflow.start())
            await executing_workflow.entry_threads_started.wait()
            # If start() failed synchronously (e.g. double-start RuntimeError
            # from _begin_workflow_run), the finally in start() set the gate
            # event so we wouldn't hang here, but the workflow never actually
            # entered IN_PROGRESS. Surface the error before falsely marking
            # submissions as running.
            if start_task.done() and start_task.exception() is not None:
                await start_task  # re-raises
            # Work has begun. Transition accepted submissions to IN_PROGRESS
            # so snapshot consumers can distinguish "accepted and queued" from
            # "actively running." Injected submissions transition when they
            # land in _inject_submission.
            for s in execution.submissions:
                if s.status is SubmissionStatus.ACCEPTED:
                    s.status = SubmissionStatus.IN_PROGRESS
            await start_task  # surfaces entry-thread startup errors
            await executing_workflow.wait_all_threads()
            # ERRORED means not every thread completed, so it also covers a PARTIAL_COMPLETED run;
            # the execution phase is the outcome a reader should use.
            any_non_completed = any(
                t.status in (
                    LabwareThreadStatus.ABORTED,
                    LabwareThreadStatus.STOPPED,
                    LabwareThreadStatus.FAILED,
                )
                or (t.status == LabwareThreadStatus.PAUSED and t.last_error is not None)
                for t in self._get_execution_threads(execution_id)
            )
            executing_workflow.status = (
                WorkflowStatus.ERRORED if any_non_completed else WorkflowStatus.COMPLETED
            )
        except Exception as exc:
            # FAILED is set at once; the end is recorded after the other threads are torn down.
            self._fail_execution_from_thread(execution_id, exc)
            raise
        finally:
            if start_task is not None:
                # No done() guard: gathering a task that already raised is what
                # retrieves its exception, and skipping that logs it as never
                # retrieved at interpreter exit.
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            executing_workflow.stop_hearing_events()
            self._system.variable_store.remove_execution(workflow_instance.id)

    def _terminal_phase_from_threads(self, execution: Execution) -> ExecutionPhase:
        """Roll up a cleanly-returned execution's phase from its threads.

        A clean task return means every thread is terminal, not that every one COMPLETED: ABORT_THREAD and a
        stop both return without raising. Any FAILED thread makes the run FAILED; a stopped thread, or no
        thread completing, makes it ABORTED; an operator-aborted thread beside a completed one makes it
        PARTIAL_COMPLETED.
        """
        ew = execution.executing_workflow
        return run_phase_of([t.status for t in (ew.threads if ew is not None else [])])

    def _fail_execution_from_thread(
        self, execution_id: str, exc: BaseException
    ) -> None:
        """Fail an execution when an entry thread raises through
        ``_run_workflow`` (e.g. the deck-site guard). A spawned thread dying
        does NOT come here: it files a THREAD_DIED incident and the rest of the
        run carries on.

        Marks FAILED now so the cancel scheduled below is not rolled back to
        ABORTED by ``_on_task_done``. Schedules the teardown that cancels the
        still-running threads, then finalizes the terminal surface here, because
        ``_on_task_done`` early-returns for a pre-set terminal phase. The end is
        recorded only once the teardown is done, so the threads' last events come
        before the run's end event.
        """
        execution = self._executions.get(execution_id)
        if execution is None or execution.phase in _TERMINAL_PHASES:
            return
        execution.phase = ExecutionPhase.FAILED
        execution.error = str(exc)
        if execution.executing_workflow is not None:
            execution.executing_workflow.status = WorkflowStatus.ERRORED
        teardown = asyncio.get_running_loop().create_task(
            self._teardown_failed_execution(execution_id)
        )
        self._background_tasks.add(teardown)
        teardown.add_done_callback(self._background_tasks.discard)
        self._finalize_execution_surface(execution, ExecutionPhase.FAILED, after=teardown)

    def _finalize_execution_surface(
        self, execution: Execution, phase: ExecutionPhase, *, after: asyncio.Task[None] | None = None,
    ) -> None:
        """End the run in order: every event it fired is saved, its final statuses and record are set, the
        submissions' and the execution's end events fire, those are saved too, and only then is it ended.
        ``after`` is work that still fires the run's events, such as a teardown; the end waits for it first.

        Runs once per execution: from ``_on_task_done`` for the phases it determines, and from
        ``_fail_execution_from_thread`` for the pre-set FAILED path, which ``_on_task_done`` then skips.
        """
        status_str = _END_STATUS.get(phase)
        ew = execution.executing_workflow
        if ew is None or status_str is None:
            execution.phase = phase
            submission_status = _END_SUBMISSION_STATUS.get(phase)
            if submission_status is not None:
                for submission in execution.submissions:
                    submission.status = submission_status
            execution.ended.set()
            self._forwarder.unregister_execution(execution.id)
            return
        ending = asyncio.get_running_loop().create_task(self._end_once_its_events_are_saved(execution, phase, after))
        self._background_tasks.add(ending)
        ending.add_done_callback(self._background_tasks.discard)
        # A shutdown can cancel the task before it starts; the run still ends.
        ending.add_done_callback(lambda _: self._end_now_if_not_ended(execution, phase))

    async def _end_once_its_events_are_saved(
        self, execution: Execution, phase: ExecutionPhase, after: asyncio.Task[None] | None,
    ) -> None:
        recorded = False
        try:
            if after is not None:
                _, still_running = await asyncio.wait({after}, timeout=END_EVENTS_SAVE_TIMEOUT_S)
                if still_running:
                    self._unsaved_records.report(
                        UnsavedRecord(
                            kind="the run's events",
                            record=(
                                f"threads of run {execution.id} still stopping after {END_EVENTS_SAVE_TIMEOUT_S}s; "
                                "their last events may come after the run's end"
                            ),
                            execution_id=execution.id,
                        ),
                        TimeoutError("the run's threads did not stop in time"),
                    )
            if not await self._event_saves.all_saved(END_EVENTS_SAVE_TIMEOUT_S):
                self._unsaved_records.report(
                    UnsavedRecord(
                        kind="the run's events",
                        record=f"events of run {execution.id} not confirmed saved in {END_EVENTS_SAVE_TIMEOUT_S}s",
                        execution_id=execution.id,
                    ),
                    TimeoutError("the event store did not confirm the run's events"),
                )
            self._record_the_end(execution, phase)
            recorded = True
            # The end event too, so a reader that sees the run ended finds it.
            await self._event_saves.all_saved(END_EVENTS_SAVE_TIMEOUT_S)
        finally:
            if not recorded:
                self._record_the_end(execution, phase)
            self._mark_ended(execution)

    def _end_now_if_not_ended(self, execution: Execution, phase: ExecutionPhase) -> None:
        if execution.ended.is_set():
            return
        self._record_the_end(execution, phase)
        self._mark_ended(execution)

    def _record_the_end(self, execution: Execution, phase: ExecutionPhase) -> None:
        """Set the final statuses, save the record, then fire the submissions' end events and the run's."""
        status_str = _END_STATUS[phase]
        execution.phase = phase
        submission_status = _END_SUBMISSION_STATUS[phase]
        for submission in execution.submissions:
            submission.status = submission_status
        ew = execution.executing_workflow
        assert ew is not None
        self._execution_record_service.mark_terminal(
            execution_id=ew.id,
            state=_phase_to_state(phase),
            reason=execution.error,
            terminal_at=datetime.now(timezone.utc),
        )
        for submission in execution.submissions:
            self._emit_submission_terminal(
                ew.id, execution.workflow_name, submission,
                status_str, reason=execution.error,
            )
        self._emit_execution_terminal(ew.id, execution.workflow_name, execution, status_str)

    def _mark_ended(self, execution: Execution) -> None:
        execution.ended.set()
        self._forwarder.unregister_execution(execution.id)

    async def _teardown_failed_execution(self, execution_id: str) -> None:
        """Cancel a fatally-failed execution's threads and task, preserving the
        already-set FAILED phase (mirrors ``abort_execution`` teardown)."""
        execution = self._executions.get(execution_id)
        if execution is None:
            return
        if execution.executing_workflow is not None:
            await execution.executing_workflow.stop_all_thread_tasks()
        if not execution.task.done():
            execution.task.cancel()

    def _on_task_done(self, execution_id: str) -> None:
        """Callback when an execution task finishes."""
        execution = self._executions.get(execution_id)
        if execution is None:
            return

        # Clear the active-executions index regardless of phase so a follow-up
        # grouped submit for this workflow boots a fresh Execution instead of
        # reusing a finished/failed/aborted one.
        if self._active_executions.get(execution.workflow_name) is execution:
            del self._active_executions[execution.workflow_name]

        if execution.phase in (ExecutionPhase.ABORTED, ExecutionPhase.FAILED):
            # Phase already set by a thread-level failure/abort. Consume the task
            # exception (if it raised) so asyncio does not warn it went unretrieved.
            if not execution.task.cancelled():
                execution.task.exception()
            # A shutdown sets ABORTED before the task ends and records nothing else; the run still has to end.
            if execution.phase is ExecutionPhase.ABORTED and not execution.ended.is_set():
                self._finalize_execution_surface(execution, ExecutionPhase.ABORTED)
            return

        task = execution.task
        if task.cancelled():
            phase = ExecutionPhase.ABORTED
            if execution.executing_workflow is not None:
                execution.executing_workflow.status = WorkflowStatus.ERRORED
        elif task.exception() is not None:
            exc = task.exception()
            phase = ExecutionPhase.FAILED
            execution.error = str(exc)
            logger.error(f"Execution {execution_id} failed: {exc}")
            if execution.executing_workflow is not None:
                execution.executing_workflow.status = WorkflowStatus.ERRORED
        else:
            phase = self._terminal_phase_from_threads(execution)
            if phase is ExecutionPhase.FAILED and execution.error is None:
                # Clean-return FAILED rollup (a crashed thread). The wire must
                # not show FAILED with a null error, and it has to name the
                # cause: this is the only place the run states why it failed.
                ew = execution.executing_workflow
                failed = [
                    t for t in (ew.threads if ew is not None else [])
                    if t.status is LabwareThreadStatus.FAILED
                ]
                execution.error = (
                    "; ".join(
                        f"thread {t.name} died on "
                        f"{type(t.last_error).__name__}: {t.last_error}"
                        if t.last_error is not None
                        else f"thread {t.name} died on an unhandled error"
                        for t in failed
                    )
                    or "a thread died on an unhandled error"
                )

        self._finalize_execution_surface(execution, phase)


async def declared_device_snapshots(registry: IDeviceRegistry, devices: IDeviceFacade) -> list[DeviceSnapshot]:
    """A snapshot of every device the topology declares, in registry order.

    A device only its orca-client reports has no snapshot to read.
    """
    return [
        await devices.get_device_status(entry.name)
        for entry in await registry.list_all()
        if entry.topology_card is not None
    ]
