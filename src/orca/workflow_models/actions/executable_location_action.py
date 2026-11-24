import asyncio
import logging
import time
from typing import Callable, List

from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import LocationActionExecutionContext, MethodExecutionContext
from orca.resource_models.devices import Device
from orca.resource_models.error_policy_overrides import WrongPartnerPlateError
from orca.resource_models.labware import LabwareInstance, LabwareTemplate
from orca.resource_models.location import Location
from orca.state.mounted import name_the_rack_a_96_return_went_to
from orca.state.records import (
    ActionContinuedDetails,
    DeclaredTracking,
    DeviceOperation,
    OperationRecord,
    TrackingRecord,
    TrackingSource,
)
from orca.resource_models.tracking_context import TrackingContext
from orca.resource_models.tracking_interpreter import IOperationInterpreter
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.variables.variable_ref import NamedRef
from orca.variables.variable_store import IVariableResolver, NullVariableResolver
from orca.workflow_models.action_state_machine import ActionEvent, ActionStateMachine
from orca.workflow_models.actions.device_call_dispatcher import CommandRecorder
from orca.workflow_models.actions.location_action import ActionBodyLocationAction
from orca.workflow_models.actions.operation_recovery import OperationDecisionSignal
from orca.workflow_models.pause_checkpoint import IPauseCheckpoint
from orca.workflow_models.status_enums import ActionStatus
from orca.workflow_models.status_manager import StatusManager
from orca.devices.interpreter_resolution import resolve_operation_interpreter

orca_logger = logging.getLogger("orca")


def _underlying_error(error: BaseException) -> BaseException:
    """The failure an operator would recognise, past the recovery-signal wrapper."""
    if isinstance(error, OperationDecisionSignal):
        return error.original_error
    return error


_TERMINAL_ACTION_STATUSES: frozenset[ActionStatus] = frozenset({
    ActionStatus.COMPLETED,
    ActionStatus.ERRORED,
    ActionStatus.ABORTED,
    ActionStatus.SKIPPED,
})

class ExecutableLocationAction:
    """Execution-context-bound LocationAction stage.

    Pre-Phase-2 this class was named ``ExecutingLocationAction``. The rename
    signals its narrowed role as a lifecycle stage rather than just a status
    routing wrapper: it owns the execution dependencies (status_manager,
    context, lls, event channel registry) plus the per-execute setup. Status
    routing lives in ``_fire(event)`` via the ``ActionStateMachine``.
    """

    def __init__(self,
                 status_manager: StatusManager,
                 action: ActionBodyLocationAction,
                 context: MethodExecutionContext,
                 variable_store: IVariableResolver | None = None,
                 event_channel_registry: EventChannelRegistry | None = None,
                 tracking_context: TrackingContext | None = None,
                 pool_indices: dict[str, int] | None = None,
                 submission_id: str | None = None,
                 *,
                 tag: str | None = None,
                 group_served: Callable[[], str | None] | None = None,
                 ) -> None:
        super().__init__()
        self._tag = tag
        self._group_served: Callable[[], str | None] = group_served or (lambda: None)
        self._status_manager = status_manager
        self._context = context
        self._submission_id = submission_id
        self._action = action
        self._variable_store: IVariableResolver = variable_store or NullVariableResolver()
        self._event_channel_registry = event_channel_registry
        self._tracking_context = tracking_context
        # Share the method's dict by reference (not ``or {}`` which copies when
        # empty): the JIT spawn sets the index after this action resolves.
        self._pool_indices = pool_indices if pool_indices is not None else {}
        self._state_machine = ActionStateMachine()
        self._pause_checkpoint: IPauseCheckpoint | None = None
        self._named_partner_barcodes: Callable[[], dict[str, str]] = dict
        self._publish_status(self._state_machine.current)
        self._is_executing = asyncio.Lock()
        # What ended this action, so a later CONTINUE can name what it carried
        # on past. Unwrapped from the recovery-signal envelope when there is one.
        self._terminal_error: BaseException | None = None

    @property
    def action(self) -> ActionBodyLocationAction:
        return self._action

    def require_partner_barcodes(self, named: Callable[[], dict[str, str]]) -> None:
        """Before the body runs, each input of a named template must carry the barcode named for it."""
        self._named_partner_barcodes = named

    def _refuse_a_partner_nobody_named(self) -> None:
        named = self._named_partner_barcodes()
        for labware in self._action.expected_inputs:
            wanted = named.get(labware.template_name)
            if wanted is not None and labware.barcode != wanted:
                raise WrongPartnerPlateError(
                    f"{self._action.command!r} at {self._action.location.name} needs the "
                    f"{labware.template_name!r} with barcode {wanted!r}, and {labware.name!r} "
                    f"(barcode {labware.barcode!r}) is the one bound to it. The step did not run. "
                    f"Put the named labware in its place, or correct the barcode, then retry."
                )

    def set_pause_checkpoint(self, checkpoint: IPauseCheckpoint) -> None:
        """Register the thread driving this action body as its pause checkpoint."""
        self._pause_checkpoint = checkpoint

    @property
    def status(self) -> ActionStatus:
        return self._state_machine.current

    def _fire(self, event: ActionEvent) -> None:
        # Two-step orchestrator: state-machine validates the transition,
        # then the publish helper writes through StatusManager. MUST stay
        # sync (no awaits between the two steps); inserting an await opens
        # a window where state_machine.current and the StatusManager
        # registry would disagree.
        new_status = self._state_machine.transition(event)
        self._publish_status(new_status)

    def _publish_status(self, status: ActionStatus) -> None:
        # Propagate thread fields from the inbound
        # MethodExecutionContext so ACTION.* events surface with
        # ``thread_id`` / ``thread_name`` / ``participating_thread_ids``
        # populated.
        id = self._action.id
        context = LocationActionExecutionContext(
            execution_id=self._context.execution_id,
            workflow_name=self._context.workflow_name,
            method_id=self._context.method_id,
            method_name=self._context.method_name,
            action_id=id,
            action_status=status.name.upper(),
            thread_id=self._context.thread_id,
            thread_name=self._context.thread_name,
            participating_thread_ids=self._context.participating_thread_ids,
            action_name=self._action.command,
        )
        self._status_manager.set_status("ACTION", id, status.name, context)

    def _resolve_variables(self) -> None:
        """Resolve NamedRef fields on the wrapped LocationAction before execute."""
        execution_id = self._context.execution_id
        for attr_name, value in vars(self._action).items():
            if isinstance(value, NamedRef):
                resolved = self._variable_store.resolve(value.name, execution_id)
                value.set_resolved_value(resolved)

    async def _execute_action(self) -> None:
        # Pre-2A these two state writes used ``self._status = ...`` on an
        # attribute that was never declared on the class -- silent dead
        # writes that pyright missed and that fired no events. 2A wires
        # them through the ActionStateMachine + StatusManager.
        self._fire(ActionEvent.LABWARE_AWAITED)
        self._action.refresh_labware_presence()
        await self.all_labware_is_present.wait()
        self._fire(ActionEvent.ACTION_STARTED)
        self._ensure_all_labware_present()
        self._refuse_a_partner_nobody_named()
        self._resolve_variables()
        self._wire_action_body_context()

        await self._action.execute()

        await self._save_declared_tracking()

    async def _save_operations(self, operations: list[OperationRecord]) -> None:
        if self._tracking_context is not None:
            ids = {labware.name: labware.id for labware in self._action.build_template_to_instance_map().values()}
            operations = await name_the_rack_a_96_return_went_to(
                operations, self._tracking_context.ops_history, ids.get,
            )
        await self._write_record(operations, declares=None)

    async def _save_declared_tracking(self) -> None:
        """What the action declared it would do, written once it has finished."""
        await self._write_record([], declares=self._action.declares)

    async def _write_record(
        self, operations: list[OperationRecord], declares: DeclaredTracking | None,
    ) -> None:
        if self._tracking_context is None:
            raise RuntimeError(f"action '{self._action.command}' has nowhere to save its operations")
        record = self._tracking_context.observer.process_operations(
            operations, self._context, self._action.id, self._resolve_thread_id_for_action(),
            declares=declares,
            template_to_instance=self._action.build_template_to_instance_map(),
        )
        if record is not None:
            record = record.model_copy(update={
                "tag": self._tag,
                "operations": [op.model_copy(update={"group_id": self._group_served()}) for op in record.operations],
            })
            await self._tracking_context.store_record(record, execution_id=self._context.execution_id)

    async def record_operator_continued(self) -> None:
        """Ledger this errored action as operator-confirmed, not as executed.

        Deliberately does NOT run the declared-tracking fold: what the action
        declared it would do is not evidence of what happened.
        """
        if self._tracking_context is None:
            return
        error_type, error_message = self._terminal_error_description()
        now = time.time()
        instances = list(self._action.build_template_to_instance_map().values())
        thread_id = self._resolve_thread_id_for_action()
        marker = OperationRecord(
            operation=DeviceOperation.ACTION_CONTINUED,
            device_name=self._action.device.name,
            affected_labware=[instance.name for instance in instances],
            affected_labware_ids=[instance.id for instance in instances],
            action_id=self._action.id,
            thread_id=thread_id,
            details=ActionContinuedDetails(
                command=self._action.command,
                error_type=error_type,
                error_message=error_message,
            ),
            timestamp=now,
            source=TrackingSource.OPERATOR,
            group_id=self._group_served(),
        )
        record = TrackingRecord(
            execution_id=self._context.execution_id,
            action_id=self._action.id,
            thread_id=thread_id,
            method_id=self._context.method_id,
            source=TrackingSource.OPERATOR,
            timestamp=now,
            operations=[marker],
            tag=self._tag,
        )
        try:
            await self._tracking_context.store_record(
                record, execution_id=self._context.execution_id,
            )
        except Exception:
            orca_logger.warning(
                "Could not ledger the operator-confirmed continue for action %s",
                self._action.command, exc_info=True,
            )

    def _terminal_error_description(self) -> tuple[str, str]:
        """Type and message of whatever ended this action.

        An action that reaches a CONTINUE has always raised, so the empty arm is
        a wiring bug rather than a runtime state. It is described rather than
        raised: a bad label must never block a recovery already in progress.
        """
        error = self._terminal_error
        if error is None:
            return "Unknown", "the action recorded no failure"
        return type(error).__name__, str(error)

    def _wire_action_body_context(self) -> None:
        self._action.set_execution_context(
            variable_store=self._variable_store,
            execution_id=self._context.execution_id,
            thread_id=self._resolve_thread_id_for_action(),
            event_channel_registry=self._event_channel_registry,
            well_selectors=self._action.well_selectors,
            event_emitter=self._status_manager.emit_event,
            workflow_name=self._context.workflow_name,
            pool_indices=self._pool_indices,
            submission_id=self._submission_id,
            pause_checkpoint=self._pause_checkpoint,
        )
        if self._tracking_context is not None:
            self._wire_recorder()

    def _resolve_thread_id_for_action(self) -> str:
        """Resolve the thread_id to stamp on this action's operation records.

        Single-thread methods: the participating tuple has exactly one
        member; the choice is unambiguous. Co-thread methods: picks the
        first tuple element, which is stable within a run (Python tuple
        iteration is insertion-ordered) but not necessarily meaningful --
        attribution lands on whichever thread the upstream wiring inserted
        first. Per-source-thread attribution on multi-thread actions
        (one record per participating thread) is a follow-up. Raises if
        the tuple is empty, which would mean the action is running outside
        any thread -- a programmer error, not a runtime condition.
        """
        if not self._context.participating_thread_ids:
            raise RuntimeError(
                f"Action '{self._action.command}' has no participating thread ids "
                "in its MethodExecutionContext; cannot stamp operation records. "
                "This is a wiring bug -- every executing action must belong to "
                "at least one thread."
            )
        return next(iter(self._context.participating_thread_ids))

    def _wire_recorder(self) -> None:
        device = self._action.device
        self._action.set_recorder(CommandRecorder(
            self._resolve_interpreter(device), self._save_operations,
            device.name, self._action.id, self._resolve_thread_id_for_action(),
        ))

    def _resolve_interpreter(self, device: Device) -> IOperationInterpreter:
        """Select interpreter for ``device`` by walking its MRO for ITrackedDevice.

        Delegates to ``resolve_operation_interpreter`` so build-time validation
        and runtime dispatch share one implementation. Devices that implement a
        tracked interface (ITrackedDevice) declare their interpreter via a
        classmethod; concrete classes do not need to override it.
        """
        return resolve_operation_interpreter(device)

    async def execute(self) -> None:

        async with self._is_executing:
            if self.status == ActionStatus.COMPLETED:
                return
            try:
                await self._execute_action()
            except asyncio.CancelledError:
                # stop_execution cancelled the owner task mid-action.
                # CancelledError is BaseException, so the except Exception arm
                # below never sees it; drive the action terminal here and
                # re-raise so the caller's cancel-and-await completes.
                if not self._is_terminal():
                    self._fire(ActionEvent.ACTION_CANCELLED)
                raise
            except Exception as e:
                self._terminal_error = _underlying_error(e)
                self._fire(ActionEvent.ACTION_ERRORED)
                raise e
            self._fire(ActionEvent.ACTION_COMPLETED)

    def _is_terminal(self) -> bool:
        return self.status in _TERMINAL_ACTION_STATUSES

    def _ensure_all_labware_present(self) -> None:
        missing_labware = self._action.peek_missing_input_labware()
        if len(missing_labware) > 0:
            raise ValueError(
                f"Missing labware for action '{self._action.command}' (ID: {self._action.id}) at location '{self._action.location}': "
                f"{', '.join([labware.name for labware in missing_labware])}"
            )
    @property
    def id(self) -> str:
        return self._action.id

    @property
    def command(self) -> str:
        return self._action.command

    @property
    def location(self) -> Location:
        return self._action.location

    @property
    def device(self) -> Device:
        return self._action.device

    @property
    def expected_inputs(self) -> List[LabwareInstance]:
        return self._action.expected_inputs

    @property
    def expected_outputs(self) -> List[LabwareInstance]:
        return self._action.expected_outputs

    def assign_input(self, template_slot: LabwareTemplate, input: LabwareInstance):
        return self._action.assign_input(template_slot, input)

    @property
    def reservation(self) -> LocationReservation:
        return self._action.reservation

    def release_reservation(self) -> None:
        return self._action.release_reservation()

    def peek_missing_input_labware(self) -> List[LabwareInstance]:
        return self._action.peek_missing_input_labware()

    def missing_input_report(self) -> List[str]:
        return self._action.missing_input_report()

    def get_present_output_labware(self) -> List[LabwareInstance]:
        return self._action.get_present_output_labware()

    @property
    def all_labware_is_present(self) -> asyncio.Event:
        return self._action.all_labware_is_present
