from abc import ABC, abstractmethod
import asyncio
import inspect
import logging
from typing import Awaitable, Callable, List, Optional
import uuid

from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import ExecutionContext
from orca.variables.variable_store import IVariableResolver, NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.device_handle import ActionRequest
from orca.workflow_models.pause_checkpoint import IPauseCheckpoint

from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.state.records import DeclaredTracking, OperationRecord
from orca.resource_models.tracking_interpreter import IOperationInterpreter
from orca.resource_models.well_selector import WellSelector
from orca.resource_models.location import ILabwareLocationObserver, LabwareLocationEvent, Location
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.device_call_dispatcher import DeviceCallDispatcher
from orca.workflow_models.actions.util import AssignedLabwareManager

orca_logger = logging.getLogger("orca")


ResidencyCheck = Callable[[str], bool]


class LocationAction(ILabwareLocationObserver, ABC):
    def __init__(self, command: str) -> None:
        self._id: str = str(uuid.uuid4())
        self._command = command
        self._reservation: LocationReservation | None = None
        self._assigned_labware_manager: AssignedLabwareManager | None = None
        self._all_labware_is_present = asyncio.Event()
        self._device: Device | None = None
        self._sites_observed = False
        self._site_correlation: dict[str, str] = {}
        self._well_selectors: dict[str, WellSelector] = {}
        self._declares: DeclaredTracking | None = None
        self._residency_check: ResidencyCheck | None = None

    def set_device(self, device: Device) -> None:
        self._device = device
        self._observe_sites()

    def set_well_selectors(self, selectors: dict[str, WellSelector]) -> None:
        self._well_selectors = selectors

    @property
    def well_selectors(self) -> dict[str, WellSelector]:
        return self._well_selectors

    def set_declares(self, declares: DeclaredTracking) -> None:
        self._declares = declares

    @property
    def declares(self) -> DeclaredTracking | None:
        return self._declares

    @property
    def assigned_labware_manager(self) -> AssignedLabwareManager:
        if self._assigned_labware_manager is None:
            raise ValueError("AssignedLabwareManager is not set.")
        return self._assigned_labware_manager

    def set_assigned_labware_manager(self, assigned_labware_manager: AssignedLabwareManager) -> None:
        self._assigned_labware_manager = assigned_labware_manager

    def set_location_reservation(self, reservation: LocationReservation) -> None:
        self._reservation = reservation
        reservation.set_membership(self._sanctions)
        reservation.reserved_location.add_observer(self)
        self._observe_sites()

    def _observe_sites(self) -> None:
        """PLACED events fire on the device's SITES, not the mutex the action
        reserves; observe them (once, whichever setter lands last) so
        co-labware arrivals reopen the gate check."""
        if self._device is None or self._reservation is None or self._sites_observed:
            return
        self._sites_observed = True
        for site in self._device.sites:
            site.add_observer(self)

    def _sanctions(self, labware_id: str) -> bool:
        """May this labware reserve one of the device's sites while we hold it?

        Yes for our own inputs, and yes for anything already standing on the
        device: the hold keeps outside labware from arriving, not inside labware
        from leaving.
        """
        return self._is_live_member(labware_id) or self._is_standing_on_the_device(labware_id)

    def _is_live_member(self, labware_id: str) -> bool:
        """Sanctioning consults the labware CURRENTLY bound
        to this action's input slots at check time - never a snapshot, so
        contributors spawned after acquisition are members the moment they
        bind."""
        manager = self._assigned_labware_manager
        if manager is None:
            return False
        return any(lw.id == labware_id for lw in manager.assigned_inputs)

    def _is_standing_on_the_device(self, labware_id: str) -> bool:
        """Is this labware already sitting on one of the device's own sites?

        Such labware was put there by an earlier action and is not an input of
        this one, so membership alone would refuse it every site it needs to
        step through on the way out - and it would sit there forever while this
        action waits for an input that needs the site it occupies. The mutex is
        there to keep OUTSIDE labware from arriving uninvited; a plate already
        inside is free to leave.
        """
        if self._device is None:
            return False
        return any(
            site.labware is not None and site.labware.id == labware_id
            for site in self._device.sites
        )

    async def notify_labware_location_change(self, event: LabwareLocationEvent, location: Location, labware: LabwareInstance) -> None:
        # A move that actuates fires PLACED; an operator asserting a position
        # fires INITIALIZED. Both mean the labware is here now.
        if event in (LabwareLocationEvent.PLACED, LabwareLocationEvent.INITIALIZED):
            self.refresh_labware_presence()

    @property
    def id(self) -> str:
        return self._id

    @property
    def location(self) -> Location:
        return self.reservation.reserved_location

    @property
    def command(self) -> str:
        return self._command

    @property
    def device(self) -> Device:
        if self._device is None:
            raise ValueError(
                f"Device not set on action '{self._command}'. "
                "The resolver must call set_device() during action resolution."
            )
        return self._device

    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        return self.assigned_labware_manager.expected_inputs

    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self.assigned_labware_manager.expected_outputs

    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance) -> None:
        self.assigned_labware_manager.assign_input(template_slot, input)

    @property
    def reservation(self) -> LocationReservation:
        if self._reservation is None:
            raise ValueError("Location reservation is not set.")
        return self._reservation

    def release_reservation(self) -> None:
        self._detach_observers()
        self.reservation.release_reservation()

    def _detach_observers(self) -> None:
        self.reservation.reserved_location.remove_observer(self)
        if self._sites_observed and self._device is not None:
            for site in self._device.sites:
                site.remove_observer(self)
        self._sites_observed = False

    def set_site_correlation(self, correlation: dict[str, str]) -> None:
        """Template-name -> site-node map for declared ``deck_positions``
        inputs; set at bind by the owning thread."""
        self._site_correlation = dict(correlation)

    def _site_of(self, labware: LabwareInstance) -> Optional[str]:
        for site in self.device.sites:
            if labware in site.loaded_labware:
                return site.position_id
        return None

    def _input_is_present(self, labware: LabwareInstance) -> bool:
        """Each input counts only at ITS OWN site: the declared
        one when ``deck_positions`` names it, any owned site otherwise. A
        plate mid-gripper sits at no site and never opens the gate."""
        if not self.device.sites:
            return labware in self.device.all_loaded_labware
        site = self._site_of(labware)
        if site is None:
            return False
        template = labware.template
        declared = (
            self._site_correlation.get(template.name) if template is not None else None
        )
        return declared is None or site == declared

    def peek_missing_input_labware(self) -> List[LabwareInstance]:
        """Assigned inputs minus currently-present labware, side-effect-free.
        Unassigned slots aren't reported here; the gate handles those via
        ``all_inputs_assigned``. Never fires the gate (reads/snapshots use it)."""
        return [
            labware
            for labware in self.assigned_labware_manager.assigned_inputs
            if not self._input_is_present(labware)
        ]

    def refresh_labware_presence(self) -> None:
        """Open the all-labware-present gate once every expected input is loaded.

        Side-effect-only and idempotent. Driven by the placed-labware observer
        and called explicitly before a co-labware wait, so labware already
        present at reservation time (no future ``placed`` event) still opens the
        gate. The pure-read counterpart is ``peek_missing_input_labware``;
        reading ``all_labware_is_present`` never triggers this (W3)."""
        if self._all_labware_is_present.is_set():
            return
        # Post-reserve JIT spawn can reach here before sibling inputs are bound;
        # stay closed until every slot is assigned, not just until loaded.
        manager = self._assigned_labware_manager
        if manager is not None and not manager.all_inputs_assigned:
            return
        if not self.peek_missing_input_labware():
            self._all_labware_is_present.set()

    def missing_input_report(self) -> List[str]:
        """Names of inputs not yet ready: assigned-but-unloaded instances plus
        still-unassigned slots. Diagnostics only (e.g. co-labware timeouts)."""
        return (
            [lw.name for lw in self.peek_missing_input_labware()]
            + self.assigned_labware_manager.unassigned_input_slot_names
        )

    def get_present_output_labware(self) -> List[LabwareInstance]:
        if not self.device.sites:
            loaded_labwares = self.device.all_loaded_labware
        else:
            loaded_labwares = [
                lw for site in self.device.sites for lw in site.loaded_labware
            ]
        present_labware: List[LabwareInstance] = []

        for labware in self.assigned_labware_manager.expected_outputs:
            if labware in loaded_labwares:
                present_labware.append(labware)
                loaded_labwares.remove(labware)

        return present_labware

    def set_residency_check(self, residency_check: ResidencyCheck | None) -> None:
        """Tell this action which of the labware on its device will never leave."""
        self._residency_check = residency_check

    def only_residents_remain(self) -> bool:
        """Is every one of THIS action's outputs still on the device a resident?

        Scoped to our own declared outputs, not to the whole device: a foreign
        plate parked on it was never ours to wait for. Reads the check set by
        ``set_residency_check``; without one, nothing counts as resident and
        this is the strict "our outputs have all left" test.
        """
        return self._non_resident_outputs_removed(None)

    def _non_resident_outputs_removed(self, exclude_labware_id: str | None = None) -> bool:
        """Drain predicate: resident labware (reused reagents,
        immovable, join-waiting stayers) never leaves, so it must not hold the
        release open. ``exclude_labware_id`` is the acquisition REQUESTER's own
        labware: it never blocks its own takeover -- granting the successor is
        exactly the handoff that lets a staying-for-next-action plate proceed."""
        check = self._residency_check
        present = [
            labware for labware in self.get_present_output_labware()
            if labware.id != exclude_labware_id
        ]
        if check is None:
            return len(present) == 0
        return all(check(labware.id) for labware in present)

    def release_when_drained(self, residency_check: ResidencyCheck | None) -> None:
        """The device mutex releases only when no non-resident
        occupants remain on owned sites. If occupants remain, the reservation is
        marked pending-drain and the MANAGER takes it over at a later
        acquisition attempt once the predicate passes (pull, not push: no armed
        trigger to go stale, and a status-dependent predicate self-heals on the
        successor's poll). The still-held membership keeps sanctioning the
        occupants' exit hops; without it a successor action's member gate
        strands them on the deck. Observers detach NOW in both branches: the
        action is complete and its placed-refresh gate is no longer relevant."""
        self.set_residency_check(residency_check)
        self._detach_observers()
        if self._non_resident_outputs_removed(None):
            self.reservation.release_reservation()
            return
        self.reservation.mark_pending_drain(self._non_resident_outputs_removed)

    @property
    def all_labware_is_present(self) -> asyncio.Event:
        return self._all_labware_is_present

    def __str__(self) -> str:
        return f"Location Action: {self.location} - {self._command}"

    @abstractmethod
    async def execute(self) -> None:
        raise NotImplementedError


def _authors_description(func: Callable[[ActionContext], Awaitable[None]]) -> str | None:
    """The action's docstring, which is the author's own account of what it does.

    An operator deciding how to recover a stopped thread is shown the action's
    function name, and a name is not a sentence. Undocumented stays None rather
    than echoing the name back, so a card can tell the two apart.
    """
    doc = func.__doc__
    if doc is None:
        return None
    return inspect.cleandoc(doc).strip() or None


class ActionBodyLocationAction(LocationAction):
    """Runs an @orca.action function body with an ActionContext.

    The function body contains device calls that execute within
    a single device reservation. Each call goes through the queue
    bridge (DeviceHandle -> ActionRequest -> device method).
    """
    def __init__(self, func: Callable[[ActionContext], Awaitable[None]], command: str) -> None:
        super().__init__(command)
        self._func = func
        self._description = _authors_description(func)
        self._variable_store: IVariableResolver = NullVariableResolver()
        # _execution_id and _thread_id are wired by ``set_execution_context``
        # before ``execute()`` runs. Optional rather than empty-string-defaulted:
        # the absent state is a legitimate construction phase, not a value to
        # propagate. ``execute()`` asserts both are populated before dispatch.
        self._execution_id: str | None = None
        self._thread_id: str | None = None
        self._submission_id: str | None = None
        self._event_channel_registry: EventChannelRegistry | None = None
        self._event_emitter: Callable[[str, ExecutionContext], None] | None = None
        self._workflow_name: str | None = None
        self._execution_well_selectors: dict[str, WellSelector] = {}
        self._pool_indices: dict[str, int] = {}
        self._pause_checkpoint: IPauseCheckpoint | None = None
        self._user_task: asyncio.Task[None] | None = None
        self._action_queue: asyncio.Queue[ActionRequest | None] | None = None
        self._operation_log: list[OperationRecord] = []
        self._interpreter: IOperationInterpreter | None = None

    @property
    def description(self) -> str | None:
        return self._description

    def set_execution_context(
        self,
        variable_store: IVariableResolver,
        execution_id: str,
        thread_id: str,
        event_channel_registry: EventChannelRegistry | None = None,
        well_selectors: dict[str, WellSelector] | None = None,
        event_emitter: Callable[[str, ExecutionContext], None] | None = None,
        workflow_name: str | None = None,
        pool_indices: dict[str, int] | None = None,
        submission_id: str | None = None,
        pause_checkpoint: IPauseCheckpoint | None = None,
    ) -> None:
        self._variable_store = variable_store
        self._execution_id = execution_id
        self._thread_id = thread_id
        self._submission_id = submission_id
        self._event_channel_registry = event_channel_registry
        self._event_emitter = event_emitter
        self._workflow_name = workflow_name
        self._execution_well_selectors = well_selectors or {}
        self._pool_indices = pool_indices or {}
        self._pause_checkpoint = pause_checkpoint

    def set_interpreter(self, interpreter: IOperationInterpreter) -> None:
        self._interpreter = interpreter

    @property
    def pending_operations(self) -> list[OperationRecord]:
        """What this action has done that is not in the record yet.

        Every op is recorded the moment its call returns; the fold happens once
        the action finishes, so an action that failed halfway is holding real
        operations no read can see.
        """
        return self._operation_log

    def drain_operation_log(self) -> list[OperationRecord]:
        log = self._operation_log
        self._operation_log = []
        return log

    def _build_labware_dict(self) -> dict[str, LabwareInstance]:
        if self._assigned_labware_manager is None:
            return {}
        labware_dict: dict[str, LabwareInstance] = {}
        for template, instance in self._assigned_labware_manager._expected_inputs.items():
            if instance is not None:
                labware_dict[template.name] = instance
        return labware_dict

    def build_template_to_instance_map(self) -> dict[str, LabwareInstance]:
        """Resolve template_name -> LabwareInstance for observers and tracking.

        DeclaredTracking dicts are keyed by template name (what the user
        writes); downstream ops_history lookups need instance names. This
        map bridges the two at action-completion time.
        """
        return self._build_labware_dict()

    def cancel_user_task(self) -> None:
        if self._user_task is not None and not self._user_task.done():
            self._user_task.cancel()
        if self._action_queue is not None:
            self._action_queue.put_nowait(None)

    @property
    def user_task_running(self) -> bool:
        """True while the action body is executing (the task cancel_user_task
        would cancel). Read by the accept-partial drain to skip aborting an
        action that is mid-flight on a real device."""
        return self._user_task is not None and not self._user_task.done()

    async def execute(self) -> None:
        # Execution context must be wired before dispatch. Without these the
        # interpreter would stamp records with empty ids and ops_history would
        # lose the attribution that search-by-thread and search-by-execution
        # need downstream.
        if self._execution_id is None or self._thread_id is None:
            raise RuntimeError(
                f"ActionBodyLocationAction '{self.command}' executed without "
                "set_execution_context() being called first; execution_id and "
                "thread_id are unset."
            )
        thread_id = self._thread_id
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        self._action_queue = queue
        ctx = ActionContext(
            device_name=self.device.name,
            action_queue=queue,
            assigned_labware=self._build_labware_dict(),
            variable_store=self._variable_store or NullVariableResolver(),
            execution_id=self._execution_id,
            event_channel_registry=self._event_channel_registry,
            well_selectors=self._execution_well_selectors,
            event_emitter=self._event_emitter,
            workflow_name=self._workflow_name,
            thread_id=self._thread_id,
            pool_indices=self._pool_indices,
            submission_id=self._submission_id,
            pause_checkpoint=self._pause_checkpoint,
        )

        async def run_user_function() -> None:
            try:
                await self._func(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                error_request = ActionRequest(
                    device_name="", command="__error__",
                    args=(), kwargs={}, error=e,
                )
                await queue.put(error_request)
                return
            await queue.put(None)

        self._user_task = asyncio.create_task(run_user_function())

        dispatcher = DeviceCallDispatcher(
            device=self.device,
            interpreter=self._interpreter,
            operation_log=self._operation_log,
            action_id=self._id,
            thread_id=thread_id,
            execution_id=self._execution_id,
        )

        try:
            while True:
                request = await queue.get()
                if request is None:
                    break
                instances = list(self._build_labware_dict().values())
                affected = [inst.name for inst in instances]
                affected_ids = [inst.id for inst in instances]
                await dispatcher.dispatch(request, affected, affected_ids, instances)
        finally:
            # Drain the user task and clear the queue handle. The inner
            # per-request ``try/except/finally`` above guarantees that
            # every request which entered the loop body has
            # ``request.completion`` set when we get here -- so a user
            # task parked on ``request.completion.wait()`` wakes at the
            # next event-loop tick, observes ``request.error``, and
            # either catches it via ``except Exception`` in the
            # ``@orca.method`` body or propagates. Either way,
            # ``run_user_function`` posts a sentinel and returns.
            #
            # We deliberately do NOT pre-emptively cancel the user task
            # before awaiting it. The prior ``self._user_task.cancel();
            # await self._user_task`` pattern won the race against the
            # just-scheduled ``completion.set()`` callback, forcing the
            # user task to observe ``CancelledError`` rather than the
            # driver's exception -- and ``except Exception`` cannot
            # catch ``CancelledError`` (BaseException-derived in 3.8+).
            #
            # ``wait_for`` bounds the drain so pathological user code
            # (e.g., a sleep AFTER the device call returned/raised)
            # cannot hang ``execute()``. On timeout it cancels the user
            # task, which is the fallback semantics the pre-existing
            # pre-cancel was reaching for. The broad ``except`` swallows
            # only inside this finally; the outer raise (the canonical
            # error surface) propagates unaltered.
            try:
                await asyncio.wait_for(self._user_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            self._action_queue = None
