"""ActionContext: the ``ctx`` object passed to @orca.action functions.

Provides access to the action's device (via DeviceHandle), labware,
variables, and event publishing within a running code action.
"""

import asyncio
import logging
import uuid as _uuid
from typing import Callable, TypeVar, cast, overload

from cheshire_drivers.labware_interfaces import IPlate, ITipRack, ITipSpot, ITrough
from pydantic import JsonValue

from orca.events.custom_emit import emit_custom_event
from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import (
    ExecutionContext,
    OperatorInstructionContext,
)
from orca.resource_models.labware import (
    LabwareInstance,
    PlateInstance,
    TipRackInstance,
    TroughInstance,
)
from orca.resource_models.well_selector import WellSelector, all_wells
from orca.variables.errors import OptionValue
from orca.variables.variable_store import IVariableResolver
from orca.workflow_models.device_handle import ActionRequest, DeviceHandle
from orca.workflow_models.pause_checkpoint import IPauseCheckpoint

T = TypeVar("T")
LI = TypeVar("LI", bound=LabwareInstance)
P = TypeVar("P", str, int, float, bool)

_operator_logger = logging.getLogger("orca.operator")


class ActionContext:
    """Execution context for an @orca.action function.

    Created per action execution. Provides:
    - ``ctx.device()`` -- DeviceHandle for the action's declared device
    - ``ctx.device(IShaker)`` -- typed handle for IDE autocomplete
    - ``ctx.labware("name")`` -- LabwareInstance lookup
    - ``ctx.next_tips("rack", 8)`` -- the next tips the rack actually holds
    - ``ctx.param("name")`` -- Variable resolution
    - ``ctx.emit("event", value, data)`` -- Publish event to other threads
    - ``ctx.wait_for("event")`` -- Wait for a named event
    """

    def __init__(
        self,
        device_name: str,
        action_queue: asyncio.Queue[ActionRequest | None],
        assigned_labware: dict[str, LabwareInstance],
        variable_store: IVariableResolver,
        execution_id: str,
        event_channel_registry: EventChannelRegistry | None = None,
        well_selectors: dict[str, WellSelector] | None = None,
        event_emitter: Callable[[str, ExecutionContext], None] | None = None,
        workflow_name: str | None = None,
        thread_id: str | None = None,
        pool_indices: dict[str, int] | None = None,
        submission_id: str | None = None,
        pause_checkpoint: IPauseCheckpoint | None = None,
    ) -> None:
        self._device_name = device_name
        self._queue = action_queue
        self._assigned_labware = assigned_labware
        self._variable_store = variable_store
        self._execution_id = execution_id
        self._event_channel_registry = event_channel_registry
        self._seen_counters: dict[str, int] = {}
        self._well_selectors = well_selectors or {}
        self._event_emitter = event_emitter
        self._workflow_name = workflow_name
        self._thread_id = thread_id
        # Reference, not ``or {}`` copy: JIT spawn sets indices post-resolve.
        self._pool_indices = pool_indices if pool_indices is not None else {}
        self._submission_id = submission_id
        self._pause_checkpoint = pause_checkpoint

    @overload
    def device(self) -> DeviceHandle: ...
    @overload
    def device(self, device_type: type[T]) -> T: ...
    def device(self, device_type: type[T] | None = None) -> DeviceHandle | T:
        """Get a handle for the action's declared device.

        No name argument needed -- each action targets a single device.
        Pass a device interface type for IDE autocomplete:
            shaker = ctx.device(IShaker)
        """
        handle = DeviceHandle(self._device_name, self._queue)
        if device_type is not None:
            return cast(T, handle)
        return handle

    def get_well_selector(self, labware_name: str) -> WellSelector:
        """Get the WellSelector for a named labware input.

        Returns all_wells() if no selector was specified.
        """
        return self._well_selectors.get(labware_name, all_wells())

    def pool_index(self, receiver_name: str) -> int:
        """0-based index of this action's contribution to the named receiver's
        current instance (first contributor -> 0); raises if it does not contribute.

        Lets a pooling action route each contributor into a distinct region, e.g.
        four 96-well plates into the four quadrants of one 384 read plate.
        """
        if receiver_name not in self._pool_indices:
            raise ValueError(
                f"Action does not contribute to receiver '{receiver_name}'. "
                f"Contributes to: {list(self._pool_indices.keys())}"
            )
        return self._pool_indices[receiver_name]

    @overload
    def labware(self, name: str) -> LabwareInstance: ...
    @overload
    def labware(self, name: str, kind: type[LI]) -> LI: ...
    def labware(self, name: str, kind: type[LabwareInstance] | None = None) -> LabwareInstance:
        """Get the LabwareInstance assigned to this action by name.

        Pass ``kind`` to narrow to a subclass (``PlateInstance``,
        ``TipRackInstance``, ``TroughInstance``); raises ``TypeError`` if
        the instance is not of that subclass.
        """
        if name not in self._assigned_labware:
            raise ValueError(
                f"Labware '{name}' not assigned to this action. "
                f"Available: {list(self._assigned_labware.keys())}"
            )
        instance = self._assigned_labware[name]
        if kind is not None and not isinstance(instance, kind):
            raise TypeError(
                f"Labware '{name}' is a {type(instance).__name__}, not {kind.__name__}"
            )
        return instance

    def plate(self, name: str) -> IPlate:
        """Return the underlying PLR plate object for a named plate input.

        Shortcut for ``ctx.labware(name, PlateInstance).plate``.
        """
        return self.labware(name, PlateInstance).plate

    def tip_rack(self, name: str) -> ITipRack:
        """Return the underlying PLR tip rack for a named tip rack input.

        Shortcut for ``ctx.labware(name, TipRackInstance).tip_rack``.
        """
        return self.labware(name, TipRackInstance).tip_rack

    async def next_tips(self, name: str, count: int) -> list[ITipSpot]:
        """The next ``count`` tip spots on this rack that still hold a tip.

        Column-major, so an 8-channel head gets a whole column. Use this rather
        than naming positions: a rack left on the deck between runs has no full
        column left by the second run, and a hard-coded one picks air.

        Raises when the rack cannot supply that many, or when nothing has ever
        said what it holds.
        """
        instance = self.labware(name, TipRackInstance)
        return [
            instance.tip_rack.tip_spot(position)
            for position in await instance.next_tips(count)
        ]

    def trough(self, name: str) -> ITrough:
        """Return the underlying PLR trough for a named trough input.

        Shortcut for ``ctx.labware(name, TroughInstance).trough``.
        """
        return self.labware(name, TroughInstance).trough

    @overload
    async def param(self, name: str) -> OptionValue: ...
    @overload
    async def param(self, name: str, kind: type[P]) -> P: ...
    async def param(self, name: str, kind: type[P] | None = None) -> OptionValue:
        """Resolve a variable by name from the execution's variable store.

        A submission's own overrides sit above the execution-wide values, so an
        action running inside one reads them first.

        Pass ``kind`` to narrow to a concrete primitive (``float``, ``int``,
        ``str``, ``bool``); raises ``TypeError`` if the resolved value is
        not of that type.
        """
        value = self._variable_store.resolve(
            name, self._execution_id, submission_id=self._submission_id,
        )
        if kind is not None and not isinstance(value, kind):
            raise TypeError(
                f"Variable '{name}' resolved to {type(value).__name__}, not {kind.__name__}"
            )
        return value

    async def wait_for(self, event_name: str, timeout: float | None = None) -> tuple[str | None, dict[str, JsonValue]]:
        """Wait for a named event. Returns (value, data).

        Uses latch semantics: sees the latest publish regardless of timing.
        """
        if self._event_channel_registry is None:
            raise RuntimeError(
                "Event channels not available. ctx.wait_for() requires an "
                "EventChannelRegistry wired through the execution pipeline."
            )
        channel = self._event_channel_registry.get_or_create(event_name)
        seen = self._seen_counters.get(event_name, 0)
        counter, value, data = await channel.wait(seen_counter=seen, timeout=timeout)
        self._seen_counters[event_name] = counter
        return value, data or {}

    async def emit(self, event_name: str, value: str | None = None, data: dict[str, JsonValue] | None = None) -> None:
        """Publish an event visible to all threads in this workflow, and to
        the runtime event surface as CUSTOM.<name>.EMITTED."""
        if self._event_channel_registry is None:
            raise RuntimeError(
                "Event channels not available. ctx.emit() requires an "
                "EventChannelRegistry wired through the execution pipeline."
            )
        channel = self._event_channel_registry.get_or_create(event_name)
        await channel.publish(value=value, data=data)
        emit_custom_event(
            self._event_emitter,
            execution_id=self._execution_id,
            workflow_name=self._workflow_name,
            thread_id=self._thread_id,
            event_name=event_name,
            value=value,
            data=data,
        )

    async def manual_step(
        self,
        instruction: str,
        timeout_hours: float = 0,
    ) -> None:
        """Pause execution and display an operator instruction.

        Logs the instruction to the 'orca.operator' logger, emits an
        OPERATOR.INSTRUCTION event with a unique step_id, and waits for
        a targeted OPERATOR.CONFIRM.{step_id} event. Each manual_step
        gets its own confirmation channel so multiple concurrent manual
        steps don't interfere.

        A manual step is a safe point: nothing is in motion and the operator
        is at the instrument. So if the execution was paused while the step
        was pending, the confirm is accepted and the thread then waits for
        resume before the rest of the body runs.

        Args:
            instruction: Human-readable instruction for the operator.
            timeout_hours: Maximum wait time in hours. 0 means no timeout
                (wait indefinitely). Raises TimeoutError if exceeded. It bounds
                the wait for the operator's confirm only: a hold for a pause
                the operator asked for themselves is not operator latency, and
                ends on their resume.
        """
        step_id = f"manual_step-{_uuid.uuid4().hex[:4]}"
        _operator_logger.info("ACTION REQUIRED [%s]: %s", step_id, instruction)
        if self._event_channel_registry is None:
            raise RuntimeError(
                "Event channels not available. ctx.manual_step() requires an "
                "EventChannelRegistry wired through the execution pipeline."
            )
        # Channel-only on purpose: the bus emit below is the typed
        # OPERATOR.INSTRUCTION; routing through self.emit would double it as CUSTOM.
        channel = self._event_channel_registry.get_or_create("OPERATOR.INSTRUCTION")
        await channel.publish(value=step_id, data={
            "instruction": instruction,
            "step_id": step_id,
        })
        self._emit_operator_instruction(instruction, step_id)
        if self._event_channel_registry is not None:
            self._event_channel_registry.record_manual_step(step_id, instruction)
        timeout = timeout_hours * 3600 if timeout_hours > 0 else None
        try:
            await self.wait_for(f"OPERATOR.CONFIRM.{step_id}", timeout=timeout)
        finally:
            if self._event_channel_registry is not None:
                self._event_channel_registry.clear_manual_step(step_id)
        if self._pause_checkpoint is not None:
            await self._pause_checkpoint.hold_if_pause_requested()

    def _emit_operator_instruction(self, instruction: str, step_id: str) -> None:
        """Broadcast OPERATOR.INSTRUCTION on the workflow event bus.

        Bridged to the SystemEventBus by the _SystemEventForwarder so an
        operating LLM is notified within wait_for_intervention. Best-effort:
        a directly-built ActionContext (no emitter wired) keeps working off
        the EventChannel path alone.
        """
        if self._event_emitter is None or self._workflow_name is None:
            return
        self._event_emitter(
            "OPERATOR.INSTRUCTION",
            OperatorInstructionContext(
                execution_id=self._execution_id,
                workflow_name=self._workflow_name,
                thread_id=self._thread_id,
                instruction=instruction,
                step_id=step_id,
            ),
        )
