"""Persistent runtime wrapping an orca-core System.

Accepts workflow submissions, tracks executions, and bridges
events to external systems via IEventSink and SystemEventBus.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Mapping, TypeVar

from cheshire_drivers.gateway_protocol import DeviceConnectInfo, DeviceStatusInfo

from orca.devices.devices import LiquidHandler
from orca.events.event_bus_interface import IEventBus
from orca.gateway.controller import device_controller
from orca.gateway.controller.exceptions import DeviceError
from orca.gateway.device_fault import DeviceFault
from orca.gateway.websocket.connection_events import connection_events
from orca.events.event_handler_interface import IEventHandler
from orca.events.execution_context import (
    DeviceFaultContext,
    ExecutionContext,
    ExecutionLifecycleContext,
    GroupLifecycleContext,
    IncidentContext,
    SubmissionExecutionContext,
    ThreadExecutionContext,
)
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin, PluginCommand
from orca.runtime.danger import (
    ActionDescriptor,
    describe_action as _describe_action_global,
    list_actions as _list_actions_global,
)
from orca.runtime.event_forwarder import _SystemEventForwarder
from orca.runtime.execution import Execution, ExecutionPhase, StopOutcome
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.facades.deck_layouts import DeckLayoutFacade
from orca.runtime.facades.devices import DeviceFacade
from orca.runtime.facades.incidents import IncidentFacade
from orca.runtime.facades.labware import LabwareFacade
from orca.runtime.facades.ops_history import OpsHistoryFacade
from orca.runtime.facades.registry import RegistryFacade
from orca.runtime.facades.submissions import SubmissionFacade
from orca.runtime.facades.teachpoints import TeachpointFacade
from orca.runtime.facades.threads import ThreadFacade
from orca.runtime.facades.variables import VariableFacade
from orca.runtime.db import create_memory_engine
from orca.runtime.execution_record_service import ExecutionRecordService
from orca.runtime.execution_tracking_sink import ExecutionTrackingSink
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    LedgerContradictionDetail,
    DeckReconcileConflictDetail,
    IncidentCategory,
    IncidentSeverity,
    RecoverableTimeoutContext,
    RecoveryAction,
    SystemIncident,
)
from orca.runtime.sqlite_execution_record_store import SqliteExecutionRecordStore
from orca.runtime.sqlite_incident_store import SqliteIncidentStore
from orca.runtime.loop_safe import deliver_on_loop
from orca.runtime.recoverable_timeout import RecoverableTimeoutCoordinator
from orca.system.reservation_manager.location_reservation import ReservationPriority
from orca.system.reservation_manager.errors import (
    ActionContinuedContext,
    ActionFailedContext,
    MoveContinuedContext,
    MoveFailedContext,
    OrphanedBacklogContext,
    ThreadDiedContext,
    UnresolvableDeadlockContext,
)
from orca.runtime.access_config_service import AccessConfigService
from orca.runtime.sqlite_access_config_store import SqliteAccessConfigStore
from orca.runtime.sqlite_move_defaults_store import SqliteMoveDefaultsStore
from orca.runtime.move_defaults_service import MoveDefaultsService
from orca.runtime.sqlite_grip_profile_store import SqliteGripProfileStore
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.registries import (
    DeviceRegistryImpl,
    NullDeviceConnectionSource,
    NullGatewayRegistry,
    SystemTopologyRegistry,
)
from orca.runtime.interfaces import (
    IAccessConfigStore,
    IDeploymentProfileStore,
    IEventSink,
    ILabwareStore,
)
from orca.runtime.profile_store import NullDeploymentProfileStore
from orca.resource_models.adhoc_labware import is_adhoc_template_name
from orca.resource_models.labware import LabwareInstance, TipRackInstance
from orca.resource_models.location import Location
from orca.runtime.labware_group import (
    AcquisitionValidationError,
    BarcodeAcquisition,
    LabwareGroup,
    LocationAcquisition,
)
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.labware_catalog_service import (
    LabwareCatalogService,
    seeded_labware_catalog_service,
)
from orca.state.projections import (
    has_tip_baseline,
    has_volume_history,
    unsettled_gaps,
)
from orca.state.provenance import Provenance
from orca.state.unsettled import (
    SettleNoun,
    UnsettledSubject,
    is_unsettled,
    settle_verb,
)
from orca.state.records import ObservationGapCause, OperationRecord
from orca.state.ops_store import SYSTEM_ID, IOpsHistoryStore, ops_for_labware
from orca.runtime.run_modes import (
    WorkflowRunMode,
    current_run_mode,
    resolve_effective_mode_for_device,
)
from orca.runtime.sim_diagnostics import (
    maybe_enable_sim_coroutine_diagnostics_from_env,
)
from orca.runtime.runtime_interface import (
    DeckLayoutRequiredError,
    IDeckLayoutFacade,
    IDeviceConnectionSource,
    IDeviceFacade,
    IDeviceRegistry,
    IGatewayRegistry,
    IIncidentFacade,
    ILabwareFacade,
    IOpsHistoryFacade,
    IRegistryFacade,
    ISubmissionFacade,
    ITeachpointFacade,
    IThreadFacade,
    ITopologyRegistry,
    IVariableFacade,
    ConcurrentLiveSimRefusedError,
    RunModeMismatchError,
    LiveSubmissionWithSimOverridesUnacknowledgedError,
    OccupiedSlot,
    RunModeRequiredError,
    StartLocationsOccupiedError,
    SubmissionBlockedByOrphanedBacklogError,
    SubmissionToPausedExecutionError,
)
from orca.runtime.runtime_state import RuntimeState
from orca.runtime.stall_detector import StallDetector, StallReport, SystemStallError, ThreadStallSnapshot, snapshot_in_flight
from orca.runtime.blockers import Blocker, derive_blockers
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
from orca.runtime.submission import BatchMode, ResolvedAcquisition, Submission, SubmissionStatus
from orca.runtime.system_event_bus import SystemEventBus
from orca.system.errors import DeviceInitializationError
from orca.system.system_interface import (
    LedgerContradiction,
    DeckConflictReason,
    DeckReconcileConflict,
    ISystem,
)
from orca.variables.errors import OptionValue
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.standalone_method_workflow import (
    StandaloneThreadSpec,
    build_standalone_method_workflow,
)
from orca.workflow_models.status_enums import LabwareThreadStatus, RecoveryDecision, WorkflowStatus
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
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


def _contradiction_message(contradiction: LedgerContradiction) -> str:
    """Say what the operator did, what the record thought, and what settles it."""
    spots = ", ".join(contradiction.positions)
    return (
        f"'{contradiction.command}' on '{contradiction.device_name}' took tips "
        f"from '{contradiction.labware_name}' at {spots}, where the record had "
        f"none: {contradiction.believed}. The command was believed and folded, "
        f"but it says nothing about the rest of the rack, so the count is now "
        f"wrong by an unknown amount. Look at the rack, then set its tip state "
        f"({contradiction.labware_id}) to what is really on it."
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
            f"An operator command on '{device}' put '{labware}' at '{where}', "
            f"where the ledger already has '{conflict.blocking_labware_name}'. "
            f"The command ran; only the record of it did not, so '{labware}' "
            f"is now at a position nothing tracks. {_SETTLE_IT}"
        )
    if conflict.reason is DeckConflictReason.CONTENTS_DIFFER:
        return (
            f"'{device}' and the record disagree about what '{labware}' at "
            f"'{where}' holds. {conflict.detail or ''} The driver holds what "
            f"orca projected onto it, so the record is the one to believe "
            f"unless someone looked. Confirm or correct it with "
            f"set-tip-state or set-well-volumes."
        ).replace("  ", " ")
    return (
        f"A gripper move of '{labware}' on '{device}' did not finish. The "
        f"driver still names '{where}', which is where the move BEGAN, and it "
        f"was going to '{driver_site}'. Look before commanding motion, then "
        f"{_SETTLE_IT[0].lower()}{_SETTLE_IT[1:]}"
    )


# Terminal thread states. A thread in any of these has finished its
# lifecycle and will not transition further; callers (mutation, pause,
# resume, recovery) treat them uniformly. Bug TTT regression: pre-fix
# only COMPLETED + STOPPED were classified terminal even though the
# enum carries ABORTED, so an operator-aborted thread sat in a half-
# state that the runtime treated as still-mutable.
_TERMINAL_THREAD_STATES = frozenset({
    LabwareThreadStatus.COMPLETED,
    LabwareThreadStatus.ABORTED,
    LabwareThreadStatus.STOPPED,
    LabwareThreadStatus.FAILED,
})


# `ExecutionStatus` lives in `status_models.py` so the runtime
# protocol (`runtime_interface.py`) can refer to it without importing
# from `system_runtime` (which would induce a cycle).


_TERMINAL_PHASES = frozenset({
    ExecutionPhase.COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.ABORTED,
})


def _phase_to_state(phase: ExecutionPhase) -> ExecutionState:
    """Map T6 ExecutionPhase to our flat ExecutionState for the daemon API.

    ACCEPTING + DRAINING + STOPPING all surface as RUNNING -- the flat
    shim represents the coarse "is this active?" distinction; STOPPING
    is a finer phase visible on the richer ExecutionStatus / DTO surface.
    Terminal states (COMPLETED/FAILED/ABORTED) share string values across
    enums, so the value-construction path works directly.
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
    unrecorded: bool = False,
    gaps: frozenset[ObservationGapCause] = frozenset(),
) -> str:
    if provenance is Provenance.UNKNOWN:
        return f"nothing has ever said what {name!r} holds, which is not the same as empty"
    if unrecorded:
        return _unrecorded_detail(f"what {name!r} holds")
    if ObservationGapCause.OPERATIONS_DROPPED in gaps:
        return _dropped_detail(f"what {name!r} holds", settle_with)
    return f"{name!r} was known, then a stretch passed with nobody watching it"


def _unrecorded_detail(subject: str) -> str:
    """An action is holding operations nobody has written down yet.

    Its own sentence because the answer is different, and because the usual one
    is actively wrong here. Confirming writes what the read shows as an
    operator's word, and what it shows is behind; stating the contents by hand
    is worse still, because the action's own operations are then folded on top
    of a number that already accounted for them. Settling the action is the
    only thing that helps, and then the numbers catch up on their own.
    """
    return (
        f"an action that has not finished did things to {subject}, and the "
        f"record only hears about them when the action ends; settle the action "
        f"(retry, continue, or abort) rather than confirming or restating this"
    )


def _dropped_detail(subject: str, settle_with: str) -> str:
    """An aborted action threw away work it had really done.

    Its own sentence because the usual one sends the operator to look for
    something a person did, and nobody did anything: the machine lost its own
    record. Looking and finding things as expected is the trap, because the
    record is short by the work the abort discarded. Only stating what is
    really there settles it, which is why the confirm verbs refuse here.
    """
    return (
        f"an action was aborted holding work nobody wrote down, so {subject} "
        f"is wrong by an amount nothing can state; look and state what is "
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
    provenance: Provenance, name: str, *, unrecorded: bool = False,
    gaps: frozenset[ObservationGapCause] = frozenset(),
) -> str:
    if provenance is Provenance.UNKNOWN:
        return f"nothing has ever said what {name!r} is carrying"
    if unrecorded:
        return _unrecorded_detail(f"what {name!r} is carrying")
    if ObservationGapCause.OPERATIONS_DROPPED in gaps:
        return _dropped_detail(
            f"what {name!r} is carrying", "set-mounted-tips",
        )
    return f"what {name!r} is carrying was known, then nobody was watching"


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
        existing_labware_name=labware.name,
        existing_template_name=labware.template_name,
    )


class SystemRuntime:
    """Long-lived runtime wrapping a built System.

    Lifecycle: CREATED -> start() -> RUNNING -> stop() -> STOPPED.
    Workflows can only be submitted while RUNNING.
    """

    def __init__(
        self,
        system: ISystem,
        labware_store: ILabwareStore | None = None,
        event_bus: IEventBus | None = None,
        incident_service: IncidentService | None = None,
        execution_record_service: ExecutionRecordService | None = None,
        access_config_service: AccessConfigService | None = None,
        move_defaults_service: MoveDefaultsService | None = None,
        grip_profile_service: GripProfileService | None = None,
        profile_store: IDeploymentProfileStore | None = None,
        gateway_registry: IGatewayRegistry | None = None,
        connection_source: IDeviceConnectionSource | None = None,
        ops_history_store: IOpsHistoryStore | None = None,
        labware_catalog_store: LabwareCatalogService | None = None,
        stall_check_interval: float | None = 10.0,
        stall_required_stable_ticks: int = 2,
    ) -> None:
        self._system = system
        self._labware_store: ILabwareStore = labware_store or InMemoryLabwareStore()
        # Built lazily on first access; disposed at shutdown only when we own it
        # (an injected catalog is the injector's to close, e.g. a Postgres one).
        self._labware_catalog_store = labware_catalog_store
        self._owns_labware_catalog_service = labware_catalog_store is None
        # Plumb the runtime's labware_store + the system ref into the
        # ExecutingWorkflowFactory so reuse-bind threads can dual-register
        # fresh labware (system.labwares + labware_store) when their
        # auto-spawn fires.
        self._system.set_executing_workflow_factory_refs(self._labware_store)
        # Source-available default: in-memory SQLite (StaticPool); schema created in start().
        # An injected service is the injector's to own; we dispose only the default.
        self._owns_incident_service = incident_service is None
        self._incident_service: IncidentService = (
            incident_service
            or IncidentService(SqliteIncidentStore(create_memory_engine()))
        )
        # Source-available default: in-memory SQLite (StaticPool); schema created in start().
        # An injected service is the injector's to own; we dispose only the default.
        self._owns_execution_record_service = execution_record_service is None
        self._execution_record_service: ExecutionRecordService = (
            execution_record_service
            or ExecutionRecordService(
                SqliteExecutionRecordStore(create_memory_engine())
            )
        )
        # Owns the recoverable-timeout race + held-dispatch registry. Seeded
        # onto the per-thread ContextVar at thread start so device dispatch can
        # park a timed-out call here for an operator decision.
        self._recoverable_timeouts = RecoverableTimeoutCoordinator(self)
        # Late-bind self as the IThreadIncidentDeclarer on the executing-
        # thread factory. Every ExecutingLabwareThread spawned after this
        # returns receives the back-ref so it can record an
        # UNRESOLVABLE_DEADLOCK incident (typed catch) and an ACTION_FAILED
        # incident (default-PAUSE action error). Must run AFTER
        # _incident_service is bound (both declarers dereference it).
        self._system.set_thread_incident_declarer(self)
        # Source-available default: in-memory SQLite (StaticPool); schema created in start().
        # An injected service is the injector's to own; we dispose only the default.
        self._owns_access_config_service = access_config_service is None
        self._access_config_service: AccessConfigService = (
            access_config_service
            or AccessConfigService(SqliteAccessConfigStore(create_memory_engine()))
        )
        self._owns_move_defaults_service = move_defaults_service is None
        self._move_defaults_service: MoveDefaultsService = (
            move_defaults_service
            or MoveDefaultsService(SqliteMoveDefaultsStore(create_memory_engine()))
        )
        self._owns_grip_profile_service = grip_profile_service is None
        self._grip_profile_service: GripProfileService = (
            grip_profile_service
            or GripProfileService(SqliteGripProfileStore(create_memory_engine()))
        )
        self._profile_store: IDeploymentProfileStore = (
            profile_store or NullDeploymentProfileStore()
        )
        self._topology_registry: ITopologyRegistry = SystemTopologyRegistry(system)
        self._gateway_registry: IGatewayRegistry = (
            gateway_registry if gateway_registry is not None else NullGatewayRegistry()
        )
        self._connection_source: IDeviceConnectionSource = (
            connection_source
            if connection_source is not None
            else NullDeviceConnectionSource()
        )
        # Unified two-card view (topology + connection sources). The
        # runtime.topology / runtime.gateway views coexist with it because
        # their consumers read different entry types; unifying them onto this
        # surface is a separate refactor.
        self._device_registry: IDeviceRegistry = DeviceRegistryImpl(
            self._topology_registry, self._connection_source, system,
        )
        # Default to the system-bound store so standalone users get a single shared
        # archive without extra wiring. A hosted deployment injects a Db-backed impl that
        # the SdkToSystemBuilder also wires onto System.ops_history; passing
        # the same instance both places keeps writes and reads coherent.
        self._ops_history_store: IOpsHistoryStore = (
            ops_history_store or system.ops_history.store
        )
        # (labware, position_id) mirrors a placement; (labware, None) a removal.
        self._location_persist_queue: asyncio.Queue[tuple[LabwareInstance, str | None]] = asyncio.Queue()
        self._location_persist_task: asyncio.Task[None] | None = None
        self._location_listener_registered = False
        self._deck_reseed_tasks: set[asyncio.Task[None]] = set()
        self._connection_listener_registered = False
        self._state = RuntimeState.CREATED
        self._executions: dict[str, Execution] = {}
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
        self._disabled_plugin_types: set[type] = set()
        self._executing_workflows: list[ExecutingWorkflow] = []

        self._system_event_bus = SystemEventBus()
        self._forwarder = _SystemEventForwarder(self._system_event_bus)
        self._event_bus = event_bus
        if event_bus is not None:
            event_bus.subscribe_all(self._forwarder)
        # Broadcast every recorded incident as an INCIDENT RuntimeEvent so
        # operator notifiers see faults without polling incidents_list.
        self._incident_service.on_record = self._emit_incident_event

        # Sub-facades. Instantiated once; property accessors return the same
        # instance across calls so a hosted deployment (later) can layer per-facade
        # org-scoping. Some facades close over self; be careful with ordering.
        self._variables = VariableFacade(system.variable_store)
        self._labware = LabwareFacade(system, self._labware_store, self)
        self._devices = DeviceFacade(
            system, self._topology_registry, self._gateway_registry,
            self._connection_source, self, device_controller,
        )
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
        )
        self._incidents = IncidentFacade(self._incident_service)
        self._teachpoints = TeachpointFacade(system)
        self._deck_layouts = DeckLayoutFacade(system)
        self._submissions = SubmissionFacade(self)
        self._ops_history = OpsHistoryFacade(self._ops_history_store)

    # -- Sub-facade properties (ISystemRuntime namespace) -------------------

    @property
    def variables(self) -> IVariableFacade:
        return self._variables

    @property
    def labware(self) -> ILabwareFacade:
        return self._labware

    @property
    def devices(self) -> IDeviceFacade:
        return self._devices

    async def unsettled_state(self) -> list[UnsettledSubject]:
        """Everything the record cannot answer, as one worklist.

        Scattered across surfaces these read as unrelated warnings; together
        they are what an operator walking up to a paused system has to settle.
        """
        unsettled: list[UnsettledSubject] = []
        unrecorded = self._system.ops_history.unrecorded
        for labware in self._system.labwares:
            # One read: `of` already folds the ops that say whether anything
            # has ever happened to this labware, and searching every execution
            # bucket twice for them is the expensive half of this call.
            ops = await self._system.labware_contents.ops_of(labware.ref)
            provenance = self._system.labware_contents.provenance_of(
                ops, labware.name,
            )
            if not is_unsettled(provenance):
                continue
            if provenance is Provenance.UNKNOWN and not ops:
                # An ordinary destination plate: its template declared nothing
                # to put in it and nothing has happened to it yet. Listing
                # every one of those is what makes the worklist unreadable.
                continue
            gaps = unsettled_gaps(ops, labware.name)
            held = unrecorded.touches_labware(labware.name)
            noun = _contents_noun(labware)
            unsettled.append(UnsettledSubject(
                subject=labware.name,
                subject_id=labware.id,
                subject_kind="labware",
                provenance=provenance,
                detail=_contents_detail(
                    provenance, labware.name,
                    settle_with=f"set-{noun}",
                    unrecorded=held,
                    gaps=gaps,
                ),
                settle_with=settle_verb(
                    noun, provenance, unrecorded=held, gaps=gaps,
                    can_be_agreed_with=_confirm_has_something_to_back_it(
                        labware, ops,
                    ),
                ),
            ))
        for device in self._liquid_handlers():
            mounted = await self._system.mounted_tips.of(device.name)
            if not is_unsettled(mounted.provenance):
                continue
            held = unrecorded.touches_device(device.name)
            unsettled.append(UnsettledSubject(
                subject=device.name,
                subject_id=None,
                subject_kind="device_head",
                provenance=mounted.provenance,
                detail=_head_detail(
                    mounted.provenance, device.name,
                    unrecorded=held, gaps=mounted.gaps,
                ),
                settle_with=settle_verb(
                    "mounted-tips", mounted.provenance, unrecorded=held,
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
    def execution_records(self) -> ExecutionRecordService:
        """The execution-record Service this runtime persists/reads executions through.

        Exposed so the daemon's get/list/detail execution operations fall back to
        the persisted terminal record when the live runtime no longer holds it.
        """
        return self._execution_record_service

    @property
    def labware_catalog_store(self) -> LabwareCatalogService:
        """The catalog Service (lock + policy) this runtime reads/writes through.

        Lazily built (SQLite-backed, seeded) when none was injected. Exposed so
        the daemon/deployment layer can share the SAME Service instance (single
        source of truth across the runtime + the operator surface).
        """
        if self._labware_catalog_store is None:
            self._labware_catalog_store = seeded_labware_catalog_service()
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
    def move_defaults_service(self) -> MoveDefaultsService:
        """The per-transporter move-parameter defaults this runtime resolves against.

        Exposed (not a facade) for the same single-source reason as
        ``access_config_store``: operator CRUD lives on the deployment layer and
        the runtime only reads this at resolution time.
        """
        return self._move_defaults_service

    @property
    def grip_profile_service(self) -> GripProfileService:
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

    def register_plugin(self, plugin: OrcaPlugin) -> None:
        """Register a plugin. Subscribes it to the SystemEventBus and injects the system."""
        self._plugins.append(plugin)
        plugin.set_system(self._system)
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

    def get_events_since(self, timestamp: float | None) -> list[RuntimeEvent]:
        """Get all events since the given timestamp. ``None`` means no filter."""
        return self._system_event_bus.get_events_since(timestamp)

    def get_events_for_execution(self, execution_id: str) -> list[RuntimeEvent]:
        """Get all events for a specific execution."""
        return self._system_event_bus.get_events_for_execution(execution_id)

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

    @property
    def plugins(self) -> list[OrcaPlugin]:
        return list(self._plugins)

    def get_plugin(self, plugin_type: type[_T]) -> _T:
        """Get a registered plugin by type. Raises KeyError if not found."""
        for p in self._plugins:
            if isinstance(p, plugin_type):
                return p
        raise KeyError(f"No plugin of type {plugin_type.__name__} registered")

    async def start(self) -> None:
        """Mark the runtime RUNNING; defer device dispatch to first-thread-touch.

        Boot does not walk LiquidHandler decks or initialize devices. Each
        execution entrypoint (`_run_workflow`, `WorkflowExecutor.start`,
        `StandaloneMethodExecutor.start`) calls
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
        await self._rehydrate_labware_locations()
        device_controller.set_fault_listener(self._on_device_fault_changed)
        # Registered AFTER rehydrate so boot re-placement (positions the store
        # already holds) does not echo back into it as writes.
        if not self._location_listener_registered:
            self._system.labware_location_service.add_update_listener(
                self._on_labware_relocated
            )
            self._system.labware_location_service.add_retire_listener(
                self._on_labware_retired
            )
            self._system.labware_location_service.add_expectation_dropped_listener(
                self._on_expectation_dropped
            )
            self._system.add_deck_reconcile_conflict_listener(
                self._on_deck_reconcile_conflict
            )
            self._system.add_ledger_contradiction_listener(
                self._on_ledger_contradiction
            )
            self._location_listener_registered = True
        # Process-scoped bus, runtime-scoped listener: shutdown() unsubscribes,
        # else a rebuilt runtime's dead predecessor keeps pushing deck state.
        # Guarded separately from the unremovable location listeners so a
        # start after shutdown resubscribes.
        if not self._connection_listener_registered:
            connection_events.subscribe_connected(
                self._on_gateway_device_connected
            )
            connection_events.subscribe_disconnected(
                self._on_gateway_device_disconnected
            )
            connection_events.subscribe_reported(
                self._on_gateway_device_reported
            )
            self._connection_listener_registered = True
        if self._location_persist_task is None:
            self._location_persist_task = asyncio.create_task(
                self._drain_labware_location_writes()
            )
        await self._validate_topology_gateway_collisions()
        # Manage schema + drain ONLY for the default we own; an injected service
        # (an injected one, process-scoped across rebuilds) is its injector's to manage.
        if self._owns_incident_service:
            await self._incident_service.ensure_schema()
            self._incident_service.start_drain_task()
        # The sink is always registered; the runtime owns event delivery even
        # when the service is injected (its drain managed by the injector).
        self.register_sink(ExecutionTrackingSink(self._execution_record_service))
        if self._owns_execution_record_service:
            await self._execution_record_service.ensure_schema()
            self._execution_record_service.start_drain_task()
        # On-loop only: ensure schema for the default we own; no drain task.
        if self._owns_access_config_service:
            await self._access_config_service.ensure_schema()
        if self._owns_move_defaults_service:
            await self._move_defaults_service.ensure_schema()
        if self._owns_grip_profile_service:
            await self._grip_profile_service.ensure_schema()
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
        rule enforced by ``DeviceRegistryImpl._verify_kind_match``. Different
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

    async def _rehydrate_labware_locations(self) -> None:
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
        for handler in self._liquid_handlers():
            await self._system.mounted_tips.note_observation_gap(
                handler.name, ObservationGapCause.RUNTIME_RESTART,
            )
        pairs = await self._labware_store.list_active_locations()
        for labware_id, position_id in pairs:
            instance = await self._labware_store.get_by_id(labware_id)
            if instance is None:
                logger.warning(
                    "Labware store referenced unknown labware_id=%r at location=%r; skipping rehydrate",
                    labware_id,
                    position_id,
                )
                continue
            await self._ensure_derived_template(instance)
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
            instance = await self._restore_persisted_instance(instance)
            # Register before placing so clear_all (iterates system.labwares)
            # can reach its slot and list_all includes it.
            self._system.add_labware(instance)
            # A labware that predates contents tracking has no opening entry;
            # give it one now rather than leaving it permanently unreadable.
            # Already-seeded labware is untouched, so a restart cannot refill a
            # consumed rack.
            await instance.enter_record(self._system.labware_contents)
            # The runtime was off. A hand could have swapped, emptied or refilled
            # this while nothing was recording, so the number stands but stops
            # counting as confirmed until someone looks.
            await self._system.labware_contents.note_observation_gap(
                instance.ref, ObservationGapCause.RUNTIME_RESTART,
            )
            # Chokepoint writes every holder so a rehydrated deck resident is
            # pickable and routable, not just slotted.
            await self._system.labware_placer.place(instance, location)

    def _on_labware_relocated(self, labware: LabwareInstance, location: Location) -> None:
        """Mirror a placement into the labware store, so a restart rehydrates
        current positions rather than registration-time ones.

        A mover's jaws are persisted like anywhere else. Recording the slot the
        plate was lifted OFF looks safer and is not: after a restart the arm is
        still holding it, and a model that says otherwise sends the arm back
        into a slot that is now empty.
        """
        self._location_persist_queue.put_nowait((labware, location.position_id))

    def _on_labware_retired(self, labware: LabwareInstance) -> None:
        """A retired labware's ACTIVE position must clear, or the next boot
        resurrects it at its end slot and blocks that slot for the next run.
        The row and its history stay: a lifecycle end is not a retraction."""
        self._location_persist_queue.put_nowait((labware, None))

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
        """A reconnected device bridge may hold a rebuilt driver session:
        forget what orca cached about its bring-up, and re-push world state.
        State only, no motion; bring-up and homing stay behind their explicit
        verbs, and the first one that runs after this now actually reaches the
        device.

        The forgetting is inline because a command can route to the returning
        device bridge from the moment its devices attach. The reseed is a
        background task, never awaited here: this listener runs inside the
        websocket handshake, BEFORE that socket's receive loop starts, and the
        reseed's gateway commands are answered only by that receive loop.
        Awaiting inline would deadlock the handshake until the command timeout
        and the reseed itself could never succeed.
        """
        del client_id
        self._forget_agent_session(device.name)
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
        that has no such per-use check.
        """
        self._devices.forget_driver_session(device_name)
        self._system.forget_bringup(device_name)

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

    async def _note_gap_for_labware_on(
        self, device_name: str, cause: ObservationGapCause,
    ) -> None:
        """Mark everything standing on this device as worth a second look.

        The device bridge rebuilt its drivers, so whatever happened while it
        was away went unrecorded. The numbers stand -- they are still the best
        there is -- but they stop counting as confirmed.
        """
        # A reconnect rebuilds the driver's own beliefs, so the head it carries
        # is exactly as unwatched as the labware standing on it.
        if any(h.name == device_name for h in self._liquid_handlers()):
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
        """Wait for every in-flight reconnect reseed to finish. Test barrier."""
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

    def _on_ledger_contradiction(self, contradiction: LedgerContradiction) -> None:
        """An operator command only made sense if the record was wrong.

        WARNING, not ERROR: nothing failed and nothing is blocked. The command
        ran, it was believed, and the labware is already marked for a person to
        state. This is the half that reaches them without their having to ask.
        """
        self._incident_service.record(
            category=IncidentCategory.LEDGER_CONTRADICTED,
            severity=IncidentSeverity.WARNING,
            message=_contradiction_message(contradiction),
            detail=LedgerContradictionDetail(
                device_name=contradiction.device_name,
                command=contradiction.command,
                labware_id=contradiction.labware_id,
                labware_name=contradiction.labware_name,
                positions=list(contradiction.positions),
                believed=contradiction.believed,
            ),
            recovery_action=RecoveryAction.NONE,
        )

    async def _drain_labware_location_writes(self) -> None:
        while True:
            labware, position_id = await self._location_persist_queue.get()
            try:
                if position_id is None:
                    await self._labware_store.clear_location(labware.id)
                    continue
                if await self._labware_store.get_by_id(labware.id) is None:
                    await self._labware_store.register(labware)
                await self._labware_store.update_location(labware.id, position_id)
            except Exception:
                # A failed mirror write must never take down a run; the ledger
                # self-corrects on the labware's next completed placement.
                logger.exception(
                    "Failed to persist location %r for labware %r",
                    position_id, labware.id,
                )
            finally:
                self._location_persist_queue.task_done()

    async def flush_labware_location_writes(self) -> None:
        """Wait until every queued position write has been attempted.

        An ordering barrier, not a success barrier: a drained write that failed
        was logged and dropped, and still counts as flushed.
        """
        await self._location_persist_queue.join()

    async def _ensure_derived_template(self, persisted: LabwareInstance) -> None:
        """Put a labware's derived template back on the system before the labware is.

        An ad-hoc template is derived from the catalog rather than declared in
        code, so a rebuilt system starts without one. Everything that reads a
        labware's template at boot needs it there first: the PLR rebuild, and
        the deck projection's ``catalog_ref``. Without this a hand-placed trough
        comes back in the ledger and not on the driver deck, which is the same
        disagreement it was placed to avoid.
        """
        if not is_adhoc_template_name(persisted.template_name):
            return
        try:
            await self._system.ensure_adhoc_labware_template(persisted.labware_type)
        except Exception:
            logger.warning(
                "Labware %s names derived template %r, which could not be rebuilt "
                "from labware type %r; the deck cannot model it",
                persisted.name, persisted.template_name, persisted.labware_type,
                exc_info=True,
            )

    async def _restore_persisted_instance(
        self, persisted: LabwareInstance,
    ) -> LabwareInstance:
        """Rebuild what the store handed back into the instance the engine expects.

        Falling back to the identity-only labware keeps a deployment that boots:
        its deck is modelled from the driver's own defaults rather than the
        declaration, which is wrong but recoverable, where a raised error here
        takes every route on the deployment to 503.
        """
        if persisted.has_plr_backing:
            return persisted
        try:
            template = self._system.get_labware_template(persisted.template_name)
        except KeyError:
            logger.warning(
                "Labware %s names template %r, which this system does not declare; "
                "leaving it identity-only",
                persisted.name, persisted.template_name,
            )
            return persisted
        try:
            return await template.restore_instance(persisted)
        except Exception:
            logger.exception(
                "Could not rebuild labware %s from template %r; leaving it identity-only",
                persisted.name, persisted.template_name,
            )
            return persisted

    async def shutdown(self, *, confirm: bool = False) -> None:
        """Abort all active executions, stop tick loops, and shut down.

        `confirm` accepted for ISystemRuntime protocol conformance; the
        operator guard lives at the CLI/REST boundary.
        """
        del confirm
        device_controller.clear_fault_listener(self._on_device_fault_changed)
        connection_events.unsubscribe_connected(self._on_gateway_device_connected)
        connection_events.unsubscribe_disconnected(
            self._on_gateway_device_disconnected
        )
        connection_events.unsubscribe_reported(self._on_gateway_device_reported)
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
        for wf in self._executing_workflows:
            wf.stop_tick_loop()
        for wf in self._executing_workflows:
            await wf.await_tick_loop_stopped()
        self._executing_workflows.clear()
        # The orphan-slot drain and the failed-execution teardown, both of
        # which write to the stores closed below.
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
            device_controller.await_cancels_sent(),
            _CANCEL_DRAIN_TIMEOUT_S,
            "cancels still on their way to the device bridges",
        )
        if self._location_persist_task is not None:
            await _bounded(
                self._location_persist_queue.join(),
                _SHUTDOWN_DRAIN_TIMEOUT_S,
                f"the location-persist queue ({self._location_persist_queue.qsize()} "
                f"positions still to write, which the next boot will not know)",
            )
            self._location_persist_task.cancel()
            try:
                await self._location_persist_task
            except asyncio.CancelledError:
                pass
            self._location_persist_task = None
        if self._owns_incident_service:
            await self._incident_service.aclose()
        if self._owns_execution_record_service:
            await self._execution_record_service.aclose()
        if self._owns_access_config_service:
            await self._access_config_service.aclose()
        if self._owns_move_defaults_service:
            await self._move_defaults_service.aclose()
        if self._owns_grip_profile_service:
            await self._grip_profile_service.aclose()
        # Read the private, not the property, so we never lazily mint a catalog
        # just to close it. Injected catalogs are the injector's to dispose.
        if self._owns_labware_catalog_service and self._labware_catalog_store is not None:
            await self._labware_catalog_store.aclose()
        await self._dispose_device_stores()
        self._state = RuntimeState.STOPPED

    async def _drain_live_executions(self) -> None:
        """Stop every live execution and wait for it, before anything closes.

        Cancelling only ``execution.task`` leaves the workflow's start task and
        its thread tasks running, because they are detached, and they keep
        working against stores this shutdown is about to close.
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

    async def _dispose_device_stores(self) -> None:
        """Dispose the per-build device store engines the runtime brought up so
        their aiosqlite worker threads do not outlive the loop. Per-build (fresh
        each mount) in the daemon; a no-op for Null/injected hosted stores."""
        for transporter in self._system.transporters:
            await transporter.teachpoint_store.aclose()
        for lh in self._liquid_handlers():
            await lh.deck_layout_store.aclose()

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
        acknowledge_warnings: bool = False,
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
          the wire contract documented on a hosted deployment's REST surface and
          the MCP ``submissions_submit`` tool. Even when an ACCEPTING
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

        # 1b. LIVE/DEVICE_SIM wire guard across executions.
        # See ConcurrentLiveSimRefusedError for why they can't overlap.
        live_sim_blocker = self._find_live_sim_conflict(mode)
        if live_sim_blocker is not None:
            raise ConcurrentLiveSimRefusedError(
                blocking_execution_id=live_sim_blocker.id,
                blocking_workflow_name=live_sim_blocker.workflow_name,
                existing_run_mode=live_sim_blocker.submissions[0].run_mode,
                submitted_run_mode=mode,
            )

        # 2. Per-device resolve via the v3.4 12-row table; collect warnings.
        warning_devices: list[
            tuple[str, WorkflowRunMode, WorkflowRunMode]
        ] = []
        for entry in self._topology_registry.list_devices():
            override = entry.sim_override
            if override is None:
                continue
            resolved = resolve_effective_mode_for_device(
                mode, override, device_name=entry.name,
            )
            if resolved.warning is not None:
                warning_devices.append((entry.name, override, resolved.resolved))

        # 4. LIVE-with-sim-override gate. Only fires for LIVE submissions
        # because the 12-row table only emits warnings on the LIVE row.
        if warning_devices and not acknowledge_warnings:
            raise LiveSubmissionWithSimOverridesUnacknowledgedError(
                warning_devices,
            )

        self._validate_group_coverage(workflow, groups)
        # Hold the join target's slot closes across the whole injection window
        # (resolve + build/register); see ExecutingWorkflow.injecting().
        async with self._injection_guard(self._join_target(workflow, groups, batch_mode)):
            resolved_acquisitions = await self._resolve_acquisitions(groups)
            self._validate_start_locations(workflow, groups, resolved_acquisitions)

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
                    execution, inject = self._build_fresh_execution(workflow, groups)
            else:
                execution, inject = self._build_fresh_execution(workflow, groups)

            submission = Submission(
                id=str(uuid.uuid4()),
                execution_id=execution.id,
                workflow_name=workflow.name,
                groups=tuple(groups),
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

            if inject:
                await self._inject_submission(execution, workflow, submission, batch_mode)

        return submission

    def _build_fresh_execution(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup],
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
        )
        self._executions[execution_id] = execution
        if groups:
            self._active_executions[workflow.name] = execution
        task.add_done_callback(lambda _t: self._on_task_done(execution_id))
        # Unregister only AFTER _on_task_done emits the terminal events (callbacks
        # fire in registration order) so those events still reach the system bus.
        task.add_done_callback(lambda _t: self._forwarder.unregister_execution(execution_id))
        return execution, False

    def _find_live_sim_conflict(
        self, mode: WorkflowRunMode,
    ) -> "Execution | None":
        """Return an alive execution that conflicts with `mode` on the wire.

        Only LIVE and DEVICE_SIM conflict: both dispatch over the wire to
        the same orca-client device connections, so a DEVICE_SIM run would
        switch a device to its sim backend while a LIVE run drives it for
        real (or vice versa), knocking it offline. PURE_SIM never reaches
        the wire, so it never conflicts. Same-mode concurrency (LIVE+LIVE,
        DEVICE_SIM+DEVICE_SIM) is fine. Walks `_executions` (the broad
        index) so groupless legacy executions are covered too.
        """
        wire_modes = {WorkflowRunMode.LIVE, WorkflowRunMode.DEVICE_SIM}
        if mode not in wire_modes:
            return None
        for execution in self._executions.values():
            if execution.task.done():
                continue
            if not execution.submissions:
                continue
            existing_mode = execution.submissions[0].run_mode
            if existing_mode in wire_modes and existing_mode is not mode:
                return execution
        return None

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
        self._emit_submission_accepted(
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
        """Enforce the coverage rules from the T6 plan's Submission Validation.

        Rules (at submit time, before accepting):
        - Group missing a member for a PER_GROUP + required=True entry thread
          template => reject.
        - Submission missing a member for a SHARED_ACROSS_GROUPS + required=True
          entry thread template => reject.
        - Member references a thread_template_name not registered in the
          workflow (neither as entry thread nor as an auto-spawn template)
          => reject. A member matches a template via EITHER the template's
          ``func_name`` (the ``@orca.thread``-decorated function name) OR
          its labware template name, since both appear naturally in author
          code.
        - Auto-spawn-only templates with no member => accept (spawned reactively).
        - ``required=False`` entry threads may be omitted.

        Does nothing when ``groups`` is empty: groupless submits are the
        legacy one-shot path and don't require coverage validation.
        """
        if not groups:
            return
        from orca.resource_models.sharing import GroupSharing

        # Collect both aliases (func_name and labware name) for each
        # registered thread so member names can use either.
        valid_names: set[str] = set()
        for t in workflow.entry_thread_templates:
            valid_names.add(t.name)
            valid_names.add(t.func_name)
        for name, t in workflow.auto_spawn_registry.items():
            valid_names.add(name)
            valid_names.add(t.func_name)

        for group in groups:
            for member in group.members:
                if member.thread_template_name not in valid_names:
                    raise AcquisitionValidationError(
                        f"Group '{group.id}' member references unknown "
                        f"thread_template_name '{member.thread_template_name}'. "
                        f"Known: {sorted(valid_names)}"
                    )

        def _member_present(group: LabwareGroup, template: ThreadTemplate) -> bool:
            return (
                group.member_for_thread(template.name) is not None
                or group.member_for_thread(template.func_name) is not None
            )

        for template in workflow.entry_thread_templates:
            if not template.required:
                continue
            sharing = template.labware_template.group_sharing
            if sharing is GroupSharing.PER_GROUP:
                for group in groups:
                    if not _member_present(group, template):
                        raise AcquisitionValidationError(
                            f"Group '{group.id}' is missing a member for required "
                            f"PER_GROUP thread template '{template.name}'."
                        )
            else:
                any_member = any(_member_present(g, template) for g in groups)
                if not any_member:
                    raise AcquisitionValidationError(
                        f"Submission is missing a member for required "
                        f"SHARED_ACROSS_GROUPS thread template '{template.name}'."
                    )

    def _safe_emit(self, event_name: str, context: ExecutionContext) -> None:
        """Emit on the workflow event bus if one is configured.

        No-op when the runtime has no event bus (tests that only use the
        system event bus still see nothing here, which matches the existing
        pattern).
        """
        if self._event_bus is None:
            return
        self._event_bus.emit(event_name, context)

    def _emit_submission_accepted(
        self, workflow_instance_id: str, workflow_name: str, submission: Submission,
    ) -> None:
        """Fire SUBMISSION.{id}.ACCEPTED on the workflow event bus."""
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
        """Fire SUBMISSION.{id}.{COMPLETED|FAILED|ABORTED} as the execution
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
        """Fire EXECUTION.{id}.{COMPLETED|FAILED|ABORTED}."""
        self._safe_emit(
            f"EXECUTION.{execution.id}.{status}",
            ExecutionLifecycleContext(
                execution_id=execution.id,
                workflow_name=workflow_name,
                reason=execution.error,
            ),
        )

    async def _resolve_acquisitions(
        self, groups: Sequence[LabwareGroup],
    ) -> dict[tuple[str, str], ResolvedAcquisition]:
        """Validate and resolve each group member's Acquisition at submit time.

        - BarcodeAcquisition: fetch the LabwareInstance from ILabwareStore;
          raise if missing.
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
        for group in groups:
            for member in group.members:
                acquisition = member.acquisition
                key = (group.id, member.thread_template_name)
                if isinstance(acquisition, BarcodeAcquisition):
                    found = await self._labware_store.get_by_barcode(acquisition.barcode)
                    if found is None:
                        raise AcquisitionValidationError(
                            f"Group '{group.id}' member '{member.thread_template_name}' "
                            f"requested BarcodeAcquisition(barcode='{acquisition.barcode}') "
                            f"but no labware with that barcode is registered in the store."
                        )
                    resolved[key] = ResolvedAcquisition(labware_instance=found)
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

    def _validate_start_locations(
        self,
        workflow: WorkflowTemplate,
        groups: Sequence[LabwareGroup],
        resolved: dict[tuple[str, str], ResolvedAcquisition],
    ) -> None:
        """Refuse the submission if any entry thread's start_location is occupied.

        Without this, `ExecutingLabwareThread.initialize_labware` retries
        `DeviceBusyError` forever on the occupied location, producing the
        silent stall. The check runs after `_resolve_acquisitions` so
        BarcodeAcquisition / LocationAcquisition overrides are visible:

        - BarcodeAcquisition resolves to an existing LabwareInstance; the
          thread does not claim a fresh slot, so the check is a no-op for
          that thread.
        - LocationAcquisition overrides the effective start_location; the
          OVERRIDE is what gets checked, not the template default.

        A later change will extend this to also skip threads whose start_spawn
        action handles its own occupancy (e.g. `reuse_existing`).
        """
        threads_by_name: dict[str, ThreadTemplate] = {
            t.name: t for t in workflow.entry_thread_templates
        }
        threads_by_name.update(
            {t.func_name: t for t in workflow.entry_thread_templates}
        )

        occupied: list[OccupiedSlot] = []

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
                    template = threads_by_name.get(member.thread_template_name)
                    if template is None:
                        continue
                    if template.start_reuse_existing:
                        continue
                    key = (group.id, member.thread_template_name)
                    res = resolved.get(key)
                    if res is not None and res.labware_instance is not None:
                        continue
                    if res is not None and res.start_location is not None:
                        _record_if_occupied(res.start_location)
                    else:
                        _record_if_occupied(template.start_location)

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
        )

    def _get_execution(self, execution_id: str) -> Execution:
        execution = self._executions.get(execution_id)
        if execution is None:
            raise KeyError(f"Execution '{execution_id}' not found")
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

        Returns `ExecutionRecord` (our flat daemon shape) rather than
        `ExecutionStatus`. Daemon routes and DTOs consume ExecutionRecord;
        the richer T6 `ExecutionStatus` is available via `get_execution_status`.
        """
        return [self.get_execution(e.id) for e in self._executions.values()]

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
        ``pause_all_threads``). Operator spawn-recovery
        (``spawn_thread_in_execution``) is exempt and still works while
        paused, by design.

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

    def remove_execution(self, execution_id: str, *, confirm: bool = False) -> None:
        """Remove a completed or aborted execution and clear its events.

        `confirm` accepted for Protocol conformance. The terminal-state guard
        surfaces as 409 at the route.
        """
        del confirm
        execution = self._get_execution(execution_id)
        state = _phase_to_state(execution.phase)
        if not state.is_terminal():
            raise RuntimeError(
                f"Execution '{execution_id}' is {state.value}; "
                f"can only remove terminal executions "
                f"(completed, failed, aborted)"
            )
        del self._executions[execution_id]
        self._system_event_bus.clear_events_for_execution(execution_id)

    def get_execution_detail(self, execution_id: str) -> ExecutionDetail:
        """Get detailed status including all thread snapshots."""
        execution = self._get_execution(execution_id)
        execution_threads = self._get_execution_threads(execution_id)
        threads = [_build_thread_snapshot(t) for t in execution_threads]
        completed = sum(1 for t in threads if t.status == "COMPLETED")
        # ABORTED counts as terminal (not active) so a single-thread
        # execution where the operator ABORT_THREAD'd doesn't report a
        # phantom in-flight thread (Bug TTT). STOPPING is in-progress
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
        )

    # -- Blocker sources (see orca.runtime.blockers) ------------------------

    async def blocker_device_snapshots(self) -> list[DeviceSnapshot]:
        """Every device and transporter, with its fault and hold state."""
        out: list[DeviceSnapshot] = []
        for entry in await self.devices.list_devices():
            try:
                out.append(self.devices.get_device_status(entry.name))
            except KeyError:
                # Connected but not declared in topology: no snapshot to read.
                continue
        return out

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
        if device_controller.fault(fault.device_id) is fault:
            await device_controller.clear_fault(fault.device_id)

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
        flight (Bug CCC).

        New threads are held before the fan-out, so a thread started after this
        call is born paused too. A parent blocked on a reservation when the
        pause lands still spawns its contributors on the way to its own safe
        point, and those threads are not in the list below; unheld they move
        labware on a run the operator stopped. The hold is recorded on the
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
            if thread.status in _TERMINAL_THREAD_STATES:
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

    def declare_unresolvable_deadlock(
        self,
        execution_id: str,
        context: UnresolvableDeadlockContext,
    ) -> SystemIncident:
        """Record an UNRESOLVABLE_DEADLOCK incident and pause every thread in the execution.

        **Round 1.5: auto-invoked.** `SystemRuntime.__init__` calls
        `self._system.set_deadlock_declarer(self)` so every
        `ExecutingLabwareThread` minted by the factory holds a
        back-reference and invokes this method from its typed
        `except UnresolvableDeadlockError:` catch (see
        `executing_labware_thread.py` near the action-resolution call).

        On invocation:
          1. Records one incident under
             `IncidentCategory.UNRESOLVABLE_DEADLOCK` with the rich
             diagnostic context (requester / blocker thread + labware
             ids, location, reason, hint). Surfaces on every operator
             surface that consumes the IncidentService:
             `orca incidents list` (CLI), `GET /api/incidents` (REST),
             `incidents_list` / `incidents_get` (MCP).
          2. Pauses the whole execution via `pause_execution`: every
             thread parks at its next safe point AND the execution-level
             latch refuses JOIN_EXISTING submissions until resume, same
             as a detected stall. The raising thread itself still goes
             through `_pause_for_error` for the error-state transition.

        Direct invocation (CLI / REST / MCP) is also supported for the
        rare case where an operator needs to declare an unresolvable
        deadlock that the engine could not detect.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        # `UnresolvableDeadlockContext` is the IncidentDetail union member
        # directly -- no separate persistence dataclass. The engine error
        # and the persisted record share one shape so adding fields to one
        # propagates automatically to the other.
        incident = self._incident_service.record(
            category=IncidentCategory.UNRESOLVABLE_DEADLOCK,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Unresolvable deadlock: thread "
                f"'{context.requesting_thread_id}' blocked on "
                f"'{context.blocking_position_id}' by immovable thread "
                f"'{context.blocking_thread_id}'."
            ),
            detail=context,
            recovery_action=RecoveryAction.THREAD_RECOVER_ABORT,
            execution_id=execution_id,
            thread_id=context.requesting_thread_id,
        )
        self.pause_execution(execution_id, reason="system", message=incident.message)
        return incident

    def declare_action_failure(
        self,
        execution_id: str,
        thread_id: str,
        context: ActionFailedContext,
    ) -> SystemIncident:
        """Record an ACTION_FAILED incident for a default-PAUSE action error.

        Auto-invoked from ``ExecutingLabwareThread._handle_action_error``
        on the PAUSE branch, right after the thread pauses itself and
        before it parks for an operator recovery decision. The thread is
        already PAUSED with ``last_error`` set; this adds the queryable
        record so the failure surfaces on ``orca incident list`` /
        ``GET /api/incidents`` / ``incidents_list`` MCP -- it does NOT
        fan out a pause (the single thread paused itself; other threads
        keep running).

        Recovery is the existing ``orca thread recover`` path, and the suggested
        verb follows ``context.device_command``. A thread suspended inside a
        device call is advised ``THREAD_RECOVER_RETRY_OP``: it reconciles
        hardware and re-runs that one call, where the whole-action retry would
        re-drive the body unreconciled and repeat the calls that already
        succeeded. Everything else is advised ``THREAD_RECOVER_RETRY``, which is
        the only retry the runtime accepts there.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        inside_call = (
            f" inside device call '{context.device_command}'"
            if context.device_command is not None else ""
        )
        return self._incident_service.record(
            category=IncidentCategory.ACTION_FAILED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Action '{context.action_command}' in method "
                f"'{context.method_name}' failed{inside_call}: "
                f"{context.error_type}: {context.error_message}"
            ),
            detail=context,
            recovery_action=(
                RecoveryAction.THREAD_RECOVER_RETRY_OP
                if context.device_command is not None
                else RecoveryAction.THREAD_RECOVER_RETRY
            ),
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_thread_death(
        self,
        execution_id: str,
        thread_id: str,
        context: ThreadDiedContext,
    ) -> SystemIncident:
        """Record a THREAD_DIED incident for a thread that stopped uncaught.

        Every fault the lab can produce parks the thread for a recovery
        decision instead. Reaching here means the error escaped that handling,
        so there is no paused thread to recover and no operator prompt: the run
        carries on without this thread and its labware sits where the crash
        left it. The incident names that position, because clearing it by hand
        is the only way it moves.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        where = (
            f" at {context.last_position_id}"
            if context.last_position_id is not None else ""
        )
        return self._incident_service.record(
            category=IncidentCategory.THREAD_DIED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Thread '{context.thread_name}' stopped on an uncaught "
                f"{context.error_type}: {context.error_message}. Its labware "
                f"'{context.labware_name}' is{where or ' wherever it stopped'} "
                f"and will not move on its own."
            ),
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_device_init_failure(
        self,
        execution_id: str,
        error: DeviceInitializationError,
    ) -> SystemIncident:
        """Record a DEVICE_INIT_FAILED incident for a device that would not come up.

        Called from ``_run_workflow`` around the lazy first-execution bring-up,
        which runs before the executing workflow exists and so before the
        thread-level incident declarer is wired. Without this the execution
        failed with nothing in ``orca incident list`` and an error string that
        did not even name the device.

        Recovery is RESTART_EXECUTION: bring-up is not resumable from here, so
        the operator fixes the device and resubmits.
        """
        from orca.runtime.incident_store import (
            DeviceInitFailedDetail,
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        return self._incident_service.record(
            category=IncidentCategory.DEVICE_INIT_FAILED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Device '{error.device_name}' failed to initialize: "
                f"{error.cause}"
            ),
            detail=DeviceInitFailedDetail(
                device_name=error.device_name,
                driver_error=str(error.cause),
            ),
            recovery_action=RecoveryAction.RESTART_EXECUTION,
            execution_id=execution_id,
        )

    def declare_action_continued(
        self,
        execution_id: str,
        thread_id: str,
        context: ActionContinuedContext,
    ) -> None:
        """Record ACTION_CONTINUED when an operator carries on past an errored action.

        The ACTION_FAILED incident for the same action stays unacknowledged:
        continuing is not resolving. This one says what the operator did and
        that nothing has re-checked the world model against the device since,
        which is why the severity is WARNING and no recovery action applies --
        there is nothing left to recover, only something to be aware of.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        self._incident_service.record(
            category=IncidentCategory.ACTION_CONTINUED,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Operator continued past errored action "
                f"'{context.action_command}' in method '{context.method_name}' "
                f"({context.error_type}: {context.error_message}). The action's "
                f"side effects are unknown; state past this point is unverified."
            ),
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_move_failure(
        self,
        execution_id: str,
        thread_id: str,
        context: MoveFailedContext,
    ) -> None:
        """Record a MOVE_FAILED incident for a default-PAUSE routing move error.

        The move-side mirror of ``declare_action_failure``: auto-invoked from
        ``ExecutingLabwareThread._execute_move_action`` on the PAUSE branch,
        after the thread pauses itself. Adds the queryable record; does NOT fan
        out a pause. Recovery is the existing ``orca thread recover`` retry.

        Always ``THREAD_RECOVER_RETRY``, never the op-level one: a move is not
        an action body suspended inside a device call, so the runtime refuses
        ``RETRY_OP`` at a move pause and whole-action retry is the only retry.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        self._incident_service.record(
            category=IncidentCategory.MOVE_FAILED,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Move of '{context.labware}' from '{context.source}' to "
                f"'{context.target}' via '{context.transporter}' failed: "
                f"{context.error_type}: {context.error_message}"
            ),
            detail=context,
            recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_move_continued(
        self,
        execution_id: str,
        thread_id: str,
        context: MoveContinuedContext,
    ) -> None:
        """Record MOVE_CONTINUED when an operator finishes a failed move by hand.

        The MOVE_FAILED incident for the same move stays as it is: the arm did
        fail. This one says the labware reached the target by hand, so an
        operator reading incidents later sees which arrival rests on a person's
        word rather than on a completed place.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        self._incident_service.record(
            category=IncidentCategory.MOVE_CONTINUED,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Operator finished the move of '{context.labware}' from "
                f"'{context.source}' to '{context.target}' by hand after "
                f"'{context.transporter}' failed ({context.error_type}: "
                f"{context.error_message}). The run continues from the target."
            ),
            detail=context,
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_unresolved_anchor_insert(
        self,
        execution_id: str,
        thread_id: str,
        anchor_name: str,
        direction: str,
        target_type: str,
        item_name: str | None,
        anchor_reached: bool,
    ) -> SystemIncident:
        """Record a WARNING UNRESOLVED_ANCHOR_INSERT incident for a dropped insert.

        Fired when a ``Before``/``After`` insert is still pending at lane
        close. ``anchor_reached`` says which drop it was: the anchor name
        never came past, or it did and the insert still never ran. Both are
        expected (conditional methods/actions skip anchors; an aborted thread
        ends with inserts still queued; an operator can anchor to a step the
        lane has gone by), so this is informational, not recoverable -- it
        only makes the otherwise-silent drop queryable.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
            UnresolvedAnchorInsertDetail,
        )
        return self._incident_service.record(
            category=IncidentCategory.UNRESOLVED_ANCHOR_INSERT,
            severity=IncidentSeverity.WARNING,
            message=(
                f"Dropped {direction}-anchor insert of {target_type} "
                f"'{item_name or '<unnamed>'}': "
                + (
                    f"'{anchor_name}' did appear, but the insert never ran"
                    if anchor_reached
                    else f"anchor '{anchor_name}' never appeared on the stream"
                )
            ),
            detail=UnresolvedAnchorInsertDetail(
                anchor_name=anchor_name,
                direction=direction,
                target_type=target_type,
                item_name=item_name or "<unnamed>",
                anchor_reached=anchor_reached,
            ),
            recovery_action=RecoveryAction.NONE,
            execution_id=execution_id,
            thread_id=thread_id,
        )

    def declare_orphaned_backlog(
        self,
        execution_id: str,
        context: OrphanedBacklogContext,
    ) -> None:
        """Record an ORPHANED_BACKLOG incident for a quarantined slot.

        The workflow already flagged the slot and pause-requested the
        in-scope threads; this adds the queryable record. Recovery is the
        EXECUTION-level resume (accept-partial): the orphaned contributions
        are abandoned and their threads continue. Per-thread resume and the
        recoverable-timeout resume path do not dispose of the backlog, and
        JOIN_EXISTING submissions are refused until the resume.
        """
        from orca.runtime.incident_store import (
            IncidentCategory,
            IncidentSeverity,
            RecoveryAction,
        )
        in_flight = (
            f" plus in-flight method '{context.in_flight_method_name}'"
            if context.in_flight_method_name is not None
            else ""
        )
        self._incident_service.record(
            category=IncidentCategory.ORPHANED_BACKLOG,
            severity=IncidentSeverity.ERROR,
            message=(
                f"Receiver '{context.receiver_thread_name}' "
                f"({context.receiver_status}) died still owing work on slot "
                f"'{context.slot_key}': {context.undelivered_count} "
                f"undelivered contribution(s){in_flight}. The slot is "
                f"quarantined and the affected threads are pausing. Resume "
                f"the EXECUTION to accept the partial fill: undelivered "
                f"contributions are abandoned and their threads continue; "
                f"work already executing or under error recovery finishes "
                f"on its own channel."
            ),
            detail=context,
            recovery_action=RecoveryAction.RESUME_EXECUTION,
            execution_id=execution_id,
            thread_id=context.receiver_thread_id,
        )

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
             ``GET /api/incidents`` (REST), ``incidents_list`` /
             ``incidents_get`` (MCP).
          2. Fans out ``pause_all_threads(execution_id)`` so every thread
             in the execution pauses at its next safe point. The
             workflow thread that submitted the overrun command is
             already blocked on the held future; pausing siblings
             prevents them from racing ahead while the operator
             deliberates.

        The operator resolves the incident through the runtime's
        ``recoverable_timeout_extend`` / ``_abort`` / ``_mark_complete``
        (exposed over REST / MCP / CLI). The coordinator settles the held
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
        threads = self._get_execution_threads(execution_id)
        resumed = 0
        pause_cancelled = 0
        error_skipped = 0
        completed_skipped = 0
        for thread in threads:
            if thread.status in _TERMINAL_THREAD_STATES:
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
        await device_controller.clear_fault(device_id)

    @property
    def recoverable_timeout_coordinator(self) -> RecoverableTimeoutCoordinator:
        """The engine's recoverable-timeout coordinator.

        Seeded onto the per-thread ``recoverable_timeout_coordinator`` ContextVar
        at thread start so device dispatch can park a timed-out call here.
        """
        return self._recoverable_timeouts

    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float,
    ) -> None:
        """Operator decision: grant the held command more time (re-arm timer)."""
        self._recoverable_timeouts.extend(incident_id, additional_seconds)

    def recoverable_timeout_abort(
        self, incident_id: str, operator: str, reason: str,
    ) -> None:
        """Operator decision: fail the held command; failure policy fires."""
        self._recoverable_timeouts.abort(incident_id, operator, reason)

    def recoverable_timeout_mark_complete(
        self, incident_id: str, operator: str, reason: str,
    ) -> None:
        """Operator decision: assert the device finished; synthesize success."""
        self._recoverable_timeouts.mark_complete(incident_id, operator, reason)

    async def spawn_thread_in_execution(
        self,
        execution_id: str,
        template_name: str,
        labware_id: str | None = None,
    ) -> ThreadSnapshot:
        """Manually create and start a thread in a live execution.

        Used for recovery from AUTO_SPAWN_FAILED incidents (the auto-spawn
        machinery couldn't find a matching thread template and paused). The
        operator names the template explicitly and optionally pins an
        existing labware instance.
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
            raise KeyError(
                f"No thread template named '{template_name}' in workflow "
                f"'{execution.workflow_name}'. Known: {known}"
            )
        labware_instance = None
        if labware_id is not None:
            found = await self._labware_store.get_by_id(labware_id)
            if found is None:
                raise KeyError(f"No labware with id '{labware_id}'")
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

        if status in _TERMINAL_THREAD_STATES:
            raise ValueError(
                f"Thread {thread.name} has status {status.name}, cannot mutate"
            )

        if status == LabwareThreadStatus.PAUSED:
            callback(self._system, thread_id)
            if thread.is_manual_paused:
                self._system.resume_thread(thread_id)
            return

        if self._event_bus is None:
            raise RuntimeError(
                "mutate_on_next_pause requires an event_bus on SystemRuntime"
            )

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

    async def _assert_deck_layouts_configured(self, mode: WorkflowRunMode) -> None:
        """Reject submissions whose liquid handlers lack a resolvable deck_layout.

        PURE_SIM: skipped. The runtime never dispatches to a real (or
        device-sim) driver, so an unconfigured deck is harmless.

        DEVICE_SIM and LIVE: every ``LiquidHandler`` in the topology must
        declare a ``deck_layout`` (non-None) AND that name must resolve to
        an entry in the device's ``deck_layout_store``. Aggregates every
        offending handler into a single ``DeckLayoutRequiredError`` rather
        than failing on the first so operators get the full picture in one
        shot.
        """
        if mode is WorkflowRunMode.PURE_SIM:
            return
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

    # -- Back-compat shims for the flat daemon API --------------------------
    # Our daemon routes and pre-T6 tests call submit_workflow/wait/get_execution
    # and consume ExecutionRecord. Dev renamed to submit/wait_for_execution/
    # get_execution_status and introduced ExecutionStatus. These shims preserve
    # our shape so routes + tests keep working; callers that want the richer
    # T6 Submission model call the new methods directly.

    async def submit_workflow(
        self,
        workflow_name: str,
        variables: Mapping[str, OptionValue] | None = None,
        deployment_profile: str | None = None,
        *,
        mode: WorkflowRunMode | None = None,
        acknowledge_warnings: bool = False,
    ) -> ExecutionRecord:
        """Submit a workflow by name, get back a flat ExecutionRecord.

        If ``deployment_profile`` names a profile registered in
        ``runtime.profile_store``, the runtime resolves it and applies its
        values via ``variable_store.load_profile`` once at submit time.
        Editing the profile via the registry afterwards does NOT affect
        the running execution; the variable store is the only resolver
        after submit.

        ``mode`` is REQUIRED at submit time. No
        deployment fallback. Before booting the execution, the runtime
        validates every device declared in topology against ``mode`` via
        ``runtime.device_registry.assert_runnable``. The static workflow
        device set is not derivable today (methods yield action templates
        from generators), so the validation operates on the topology's
        declared devices: every declared device must be runnable under
        ``mode``. Anything the workflow references mid-execution that is
        missing or unconnected fails at action-spawn time.

        ``acknowledge_warnings`` (default ``False``) suppresses the
        ``LIVE_SUBMISSION_WITH_SIM_OVERRIDES_UNACKNOWLEDGED`` gate. CLI
        maps this to ``--confirm``.
        """
        template = self._system.get_workflow_template(workflow_name)
        return await self._submit_template(
            template, variables, deployment_profile,
            mode=mode, acknowledge_warnings=acknowledge_warnings,
        )

    async def _submit_template(
        self,
        template: WorkflowTemplate,
        variables: Mapping[str, OptionValue] | None,
        deployment_profile: str | None,
        *,
        mode: WorkflowRunMode | None,
        acknowledge_warnings: bool,
    ) -> ExecutionRecord:
        """Shared submit tail for submit_workflow and submit_method.

        The two differ only in how they obtain ``template`` (registry
        lookup vs a synthesized one-method workflow); mode validation,
        submission, variable-store wiring, profile load, and record
        construction are identical.
        """
        if mode is None:
            raise RunModeRequiredError()
        topology_names = [
            entry.name for entry in self._topology_registry.list_devices()
        ]
        await self._device_registry.assert_runnable(topology_names, mode)
        await self._assert_deck_layouts_configured(mode)
        submission = await self.submit(
            template,
            variables=variables or {},
            deployment_profile=deployment_profile,
            mode=mode,
            acknowledge_warnings=acknowledge_warnings,
        )
        execution = self._executions[submission.execution_id]
        # Eager-register so profile resolution (between submit and
        # _run_workflow) finds the partition; _run_workflow's call is idempotent.
        self._system.variable_store.create_execution(
            submission.execution_id, submission.workflow_name,
        )
        if deployment_profile is not None:
            profile = await self._profile_store.get(deployment_profile)
            if profile is None:
                raise KeyError(
                    f"DeploymentProfile {deployment_profile!r} not found",
                )
            self._system.variable_store.load_profile(
                submission.execution_id, profile,
            )
        return ExecutionRecord(
            id=submission.execution_id,
            workflow_name=submission.workflow_name,
            status=_phase_to_state(execution.phase),
            error=execution.error,
        )

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
        acknowledge_warnings: bool = False,
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
            variables, deployment_profile, mode: Same semantics as
                ``submit_workflow``.

        Raises:
            KeyError: workflow_name unknown, method_name not in that
                workflow's bundle, or a labware template name doesn't
                resolve.
        """
        parent_workflow = self._system.get_workflow_template(workflow_name)
        method_template = next(
            (m for m in parent_workflow.bundled_methods if m.name == method_name),
            None,
        )
        if method_template is None:
            available = sorted(m.name for m in parent_workflow.bundled_methods)
            raise KeyError(
                f"Method '{method_name}' not found in workflow "
                f"'{workflow_name}' bundle. Available: {available}"
            )
        if not isinstance(method_template, MethodTemplate):
            raise TypeError(
                f"bundled method '{method_name}' is not a MethodTemplate "
                f"(got {type(method_template).__name__})"
            )

        # Start/end must map the same labware; an unmatched key would hang on
        # co-labware that never arrives (action inputs aren't statically known).
        if set(labware_start) != set(labware_end):
            only_start = sorted(set(labware_start) - set(labware_end))
            only_end = sorted(set(labware_end) - set(labware_start))
            raise KeyError(
                "labware_start and labware_end must map the same labware; "
                f"start-only={only_start}, end-only={only_end}"
            )

        labware_template_index = {
            t.name: t for t in self._system.labware_templates
        }
        threads: list[StandaloneThreadSpec] = []
        for lw_name, start_loc_name in labware_start.items():
            if lw_name not in labware_template_index:
                raise KeyError(
                    f"Labware template '{lw_name}' not found. Available: "
                    f"{sorted(labware_template_index)}"
                )
            threads.append((
                labware_template_index[lw_name],
                self._system.get_location(start_loc_name),
                self._system.get_location(labware_end[lw_name]),
            ))

        # Only the first thread is an entry thread; the rest are auto-spawned,
        # so _validate_start_locations would never look at them and an occupied
        # slot for thread 2..N would spin in the spawn retry until the stall
        # detector fired. Every start location this call routes to is its own.
        occupied = [
            slot for slot in (_occupied_slot(start) for _, start, _ in threads)
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
        )

        # Carry the parent's variable definitions so method bodies relying on
        # workflow-scoped defaults resolve and explicit values type-validate.
        if parent_workflow.variable_definitions:
            self._system.variable_store.register_workflow_definitions(
                synthetic_name, parent_workflow.variable_definitions,
            )

        return await self._submit_template(
            synthetic, variables, deployment_profile,
            mode=mode, acknowledge_warnings=acknowledge_warnings,
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
        # Drain the call_soon ``_on_task_done`` callback (it flips phase to the
        # terminal value) before reading, else a stopped exec reads STOPPING.
        await asyncio.sleep(0)
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

    def disable_plugin(self, plugin_type: type[OrcaPlugin]) -> None:
        """Mark a plugin type disabled without unregistering it."""
        self._disabled_plugin_types.add(plugin_type)

    def enable_plugin(self, plugin_type: type[OrcaPlugin]) -> None:
        """Mark a plugin type enabled (default state)."""
        self._disabled_plugin_types.discard(plugin_type)

    def list_plugins(self) -> list[PluginSnapshot]:
        return [
            PluginSnapshot(
                type_name=type(p).__name__,
                disabled=type(p) in self._disabled_plugin_types,
                exposed_command_names=tuple(c.name for c in p.get_commands()),
            )
            for p in self._plugins
        ]

    def list_plugin_commands(self) -> list[PluginCommand]:
        """Aggregate the command surface exposed by registered plugins."""
        commands: list[PluginCommand] = []
        for plugin in self._plugins:
            commands.extend(plugin.get_commands())
        return commands

    async def execute_plugin_command(self, name: str, args: list[str]) -> str:
        """Dispatch to the first matching plugin command's async handler."""
        for plugin in self._plugins:
            for cmd in plugin.get_commands():
                if cmd.name == name:
                    return await cmd.handler(args)
        raise KeyError(f"No plugin command named '{name}'")

    # -- Danger-action registry + event subscribe --------------------------

    def describe_action(self, action_name: str) -> ActionDescriptor:
        return _describe_action_global(action_name)

    def list_actions(self) -> List[ActionDescriptor]:
        return _list_actions_global()

    async def subscribe_events(
        self,
        since: float | None = None,
        execution_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Async generator over RuntimeEvents. Used by the SSE route.

        Replays events since `since` (or from the start when None), then
        streams live events until the caller closes the generator. If
        `execution_id` is given, only events for that execution are emitted.
        """
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()

        def _deliver(event: RuntimeEvent) -> None:
            if execution_id is not None and event.execution_id != execution_id:
                return
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event)

        def sink(event: RuntimeEvent) -> None:
            # The bus emits off-loop for incidents; marshal onto this loop so
            # the asyncio.Queue is only touched on-loop.
            deliver_on_loop(loop, _deliver, event)

        # Replay backlog
        backlog = (
            self.get_events_for_execution(execution_id)
            if execution_id is not None
            else self.get_events_since(since)
        )
        for event in backlog:
            if since is None or event.timestamp >= since:
                yield event

        self._system_event_bus.subscribe(sink)
        try:
            while True:
                yield await queue.get()
        finally:
            self._system_event_bus.unsubscribe(sink)

    def _get_execution_threads(self, execution_id: str) -> list[ExecutingLabwareThread]:
        """Get threads belonging to a specific execution."""
        entry = self._get_execution(execution_id)
        if entry.executing_workflow is not None:
            return entry.executing_workflow.threads
        return []

    def _find_thread_template(self, workflow_name: str, template_name: str):
        """Look up a ThreadTemplate by its labware name or func_name.

        Authors register thread templates via @orca.thread; the internal
        key is the labware_template.name, but authors naturally use the
        decorated function's name. Accept either to match the Submission
        API's lookup convention. Thread names are unique per workflow, so
        the search is scoped to the spawning execution's workflow.
        """
        templates = self._system.get_labware_thread_templates()
        direct = templates.get((workflow_name, template_name))
        if direct is not None:
            return direct
        for (wf_name, _name), template in templates.items():
            if wf_name == workflow_name and template.func_name == template_name:
                return template
        return None

    def _find_thread(self, execution_id: str, thread_id: str) -> ExecutingLabwareThread:
        """Look up an ExecutingLabwareThread by execution and thread ID."""
        for thread in self._get_execution_threads(execution_id):
            if thread.id == thread_id:
                return thread
        raise KeyError(f"Thread '{thread_id}' not found in execution '{execution_id}'")

    async def _run_workflow(self, execution_id: str, workflow: WorkflowTemplate) -> None:
        """Background coroutine: build workflow instance and execute.

        Inlines WorkflowExecutor logic to capture workflow_instance_id
        for the event forwarder mapping before execution starts. Per-group
        entry threads are spawned (one per group per entry template) and
        tagged with group_id + submission_id for group-aware slot keying.

        Every execution is created by `submit()`, which appends the boot
        submission before the task scheduled here is allowed to run, so
        `execution.submissions[0]` is always present. The submission's
        `run_mode` seeds `current_run_mode` for this task; child thread
        tasks inherit it via asyncio context propagation. The first
        execution under each run mode to reach
        `ensure_runtime_initialized` triggers the lazy first-thread-touch
        walk for that mode's world (LH deck configure + fresh non-sim
        device-world init); later executions under the same mode no-op.
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
            await self._system.ensure_runtime_initialized(workflow)
        except DeviceInitializationError as exc:
            self.declare_device_init_failure(execution_id, exc)
            raise

        # Pass id=execution_id so workflow_instance.id equals execution_id.
        # Unified-id model: one UUID per execution on the T6 submission path,
        # letting the event forwarder stay a set-membership check.
        workflow_instance = await self._system.create_and_register_workflow_instance(
            workflow, submission_id=submission_id, groups=groups,
            batch_mode=batch_mode,
            resolved_acquisitions=resolved_acquisitions,
            id=execution_id,
            run_mode=run_mode,
        )
        self._forwarder.register_execution(execution_id)

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
        executing_workflow.set_incident_declarer(self)

        # SUBMISSION.ACCEPTED for the boot submission fires now that the
        # workflow event bus + forwarder mapping is wired.
        self._emit_submission_accepted(workflow_instance.id, workflow.name, submission)

        # Held out here so the finally can reach it: nothing else owns this
        # task between its creation and the await below.
        start_task: asyncio.Task[None] | None = None
        try:
            # Schedule entry threads as a background task so the IN_PROGRESS
            # transition fires when work BEGINS rather than when entry threads
            # complete. ExecutingWorkflow.start() awaits gather() over entry-
            # thread coroutines internally; previously _run_workflow awaited
            # start() directly, so SubmissionStatus stayed at ACCEPTED until
            # every entry thread terminated -- 80s+ on real hardware.
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
            # Keep the workflow status in step with the execution phase roll-up
            # (_terminal_phase_from_threads): a thread that ABORTED or STOPPED
            # makes the run non-COMPLETED. Checking only PAUSED-with-error left
            # executing_workflow.status=COMPLETED while execution.phase=ABORTED,
            # so the two fields disagreed for any operator-aborted thread.
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
            # Set FAILED synchronously + async teardown (like the spawned-contributor
            # path) so a contributor parked at co-labware can't block the failure.
            self._fail_execution_from_thread(execution_id, exc)
            raise
        finally:
            if start_task is not None:
                # No done() guard: gathering a task that already raised is what
                # retrieves its exception, and skipping that logs it as never
                # retrieved at interpreter exit.
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            self._system.variable_store.remove_execution(workflow_instance.id)

    def _terminal_phase_from_threads(self, execution: Execution) -> ExecutionPhase:
        """Roll up a cleanly-returned execution's phase from its threads.

        A clean task return means every thread reached a terminal state, but
        not necessarily COMPLETED: ABORT_THREAD and a cooperative stop both
        return without raising. The execution is COMPLETED only if every thread
        COMPLETED; any ABORTED or STOPPED thread makes it ABORTED.
        """
        ew = execution.executing_workflow
        threads = ew.threads if ew is not None else []
        # Load-bearing: a crashed thread sets ``completed`` via FAILED, so the
        # clean-return rollup reaches here.
        if any(t.status is LabwareThreadStatus.FAILED for t in threads):
            return ExecutionPhase.FAILED
        if any(
            t.status in (LabwareThreadStatus.ABORTED, LabwareThreadStatus.STOPPED)
            for t in threads
        ):
            return ExecutionPhase.ABORTED
        return ExecutionPhase.COMPLETED

    def _fail_execution_from_thread(
        self, execution_id: str, exc: BaseException
    ) -> None:
        """Fail an execution when an entry thread raises through
        ``_run_workflow`` (e.g. the deck-site guard). A spawned thread dying
        does NOT come here: it files a THREAD_DIED incident and the rest of the
        run carries on.

        Marks FAILED now so the cancel scheduled below is not rolled back to
        ABORTED by ``_on_task_done``; finalizes the terminal surface here (maps
        submissions to FAILED and emits the terminal events) because
        ``_on_task_done`` early-returns for a pre-set terminal phase and would
        otherwise leave the record RUNNING and clients unnotified; then schedules
        the async teardown that cancels the still-waiting contributors.
        """
        execution = self._executions.get(execution_id)
        if execution is None or execution.phase in (
            ExecutionPhase.COMPLETED,
            ExecutionPhase.FAILED,
            ExecutionPhase.ABORTED,
        ):
            return
        execution.phase = ExecutionPhase.FAILED
        execution.error = str(exc)
        if execution.executing_workflow is not None:
            execution.executing_workflow.status = WorkflowStatus.ERRORED
        self._finalize_execution_surface(execution)
        teardown = asyncio.get_running_loop().create_task(
            self._teardown_failed_execution(execution_id)
        )
        self._background_tasks.add(teardown)
        teardown.add_done_callback(self._background_tasks.discard)

    def _finalize_execution_surface(self, execution: Execution) -> None:
        """Map every submission to the execution's terminal phase and emit the
        terminal submission + execution events. Runs once per execution: from
        ``_on_task_done`` for the phases it determines, and from
        ``_fail_execution_from_thread`` for the pre-set FAILED escalation path
        (which ``_on_task_done`` then skips, so there is no double-emit). Emission
        is a no-op when no workflow event bus is wired."""
        submission_map = {
            ExecutionPhase.COMPLETED: SubmissionStatus.COMPLETED,
            ExecutionPhase.FAILED: SubmissionStatus.FAILED,
            ExecutionPhase.ABORTED: SubmissionStatus.ABORTED,
        }
        mapped = submission_map.get(execution.phase)
        if mapped is not None:
            for submission in execution.submissions:
                submission.status = mapped

        ew = execution.executing_workflow
        if ew is None:
            return
        phase_to_status = {
            ExecutionPhase.COMPLETED: "COMPLETED",
            ExecutionPhase.FAILED: "FAILED",
            ExecutionPhase.ABORTED: "ABORTED",
        }
        status_str = phase_to_status.get(execution.phase)
        if status_str is None:
            return
        for submission in execution.submissions:
            self._emit_submission_terminal(
                ew.id, execution.workflow_name, submission,
                status_str, reason=execution.error,
            )
        self._emit_execution_terminal(
            ew.id, execution.workflow_name, execution, status_str,
        )

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
            return

        task = execution.task
        if task.cancelled():
            execution.phase = ExecutionPhase.ABORTED
            if execution.executing_workflow is not None:
                execution.executing_workflow.status = WorkflowStatus.ERRORED
        elif task.exception() is not None:
            exc = task.exception()
            execution.phase = ExecutionPhase.FAILED
            execution.error = str(exc)
            logger.error(f"Execution {execution_id} failed: {exc}")
            if execution.executing_workflow is not None:
                execution.executing_workflow.status = WorkflowStatus.ERRORED
        else:
            execution.phase = self._terminal_phase_from_threads(execution)
            if execution.phase is ExecutionPhase.FAILED and execution.error is None:
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

        self._finalize_execution_surface(execution)
