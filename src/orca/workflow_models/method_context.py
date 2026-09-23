"""MethodContext: the ``ctx`` object passed to @orca.method functions.

Provides access to devices (via DeviceHandle), labware, variables,
and cross-thread event publishing within a running code method.
"""

import asyncio
from typing import TypeVar, cast, overload

from pydantic import JsonValue

from orca.events.custom_emit import EventEmitter, emit_custom_event
from orca.events.event_channel import EventChannelRegistry
from orca.resource_models.labware import LabwareInstance
from orca.variables.errors import OptionValue
from orca.variables.variable_store import IVariableResolver
from orca.workflow_models.device_handle import ActionRequest, DeviceHandle

T = TypeVar("T")


class MethodContext:
    """Execution context for a code method function.

    Created per method execution. Provides:
    - ``ctx.device("name")`` -- DeviceHandle for issuing actions
    - ``ctx.labware("name")`` -- LabwareInstance lookup
    - ``ctx.param("name")`` -- Variable resolution
    - ``ctx.emit("event", value, data)`` -- Publish event to other threads
    """

    def __init__(
        self,
        action_queue: asyncio.Queue[ActionRequest | None],
        assigned_labware: dict[str, LabwareInstance],
        variable_store: IVariableResolver,
        execution_id: str,
        event_channel_registry: EventChannelRegistry | None = None,
        submission_id: str | None = None,
        event_emitter: EventEmitter | None = None,
        workflow_name: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        self._queue = action_queue
        self._assigned_labware = assigned_labware
        self._variable_store = variable_store
        self._execution_id = execution_id
        self._event_channel_registry = event_channel_registry
        self._submission_id = submission_id
        self._event_emitter = event_emitter
        self._workflow_name = workflow_name
        self._thread_id = thread_id

    @overload
    def device(self, name: str) -> DeviceHandle: ...
    @overload
    def device(self, name: str, device_type: type[T]) -> T: ...
    def device(self, name: str, device_type: type[T] | None = None) -> DeviceHandle | T:
        """Get a handle for the named device.

        Pass a device interface type for IDE autocomplete:
            sealer = ctx.device("sealer_1", ISealer)
        """
        handle = DeviceHandle(name, self._queue)
        if device_type is not None:
            return cast(T, handle)
        return handle

    def labware(self, name: str) -> LabwareInstance:
        """Get the LabwareInstance assigned to this method by name."""
        if name not in self._assigned_labware:
            raise ValueError(
                f"Labware '{name}' not assigned to this method. "
                f"Available: {list(self._assigned_labware.keys())}"
            )
        return self._assigned_labware[name]

    async def param(self, name: str) -> OptionValue:
        """Resolve a variable by name from the execution's variable store.

        A submission's own overrides sit above the execution-wide values, so a
        method running inside one reads them first.
        """
        return self._variable_store.resolve(
            name, self._execution_id, submission_id=self._submission_id,
        )

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
