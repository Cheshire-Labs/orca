"""The COMMAND events of each device command, sent once the command is saved.

Every device command runs inside `one_command`, opened by `DeviceCommandPipeline` around the command and its
records. Its STARTED event goes out when the device takes the command, and its end event when the block
ends, after every record, so a command whose save fails ends FAILED. These events are the one
source of command events for every watcher.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from pydantic import BaseModel, JsonValue

from orca.events.execution_context import DeviceCommandContext
from orca.events.runtime_event import RuntimeEvent
from orca.gateway.controller.exceptions import instrument_outcome_of
from orca.events.emitter import IEventEmitter
from orca.modes.run_modes import current_thread
from orca.state.ops_store import SYSTEM_ID

logger = logging.getLogger(__name__)

ErrorText = Callable[[BaseException], str]


def plain_error(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def emit_command(events: IEventEmitter, context: DeviceCommandContext, status: str) -> None:
    """Send one COMMAND event. A stream that fails never fails the command."""
    try:
        events.emit(RuntimeEvent(
            event_name=f"COMMAND.{context.device_name}.{status}",
            execution_id=context.execution_id or SYSTEM_ID,
            timestamp=time.time(),
            entity_type="COMMAND",
            entity_id=context.device_name,
            status=status,
            context=context,
        ))
    except Exception:
        logger.warning("could not send the %s event for %s on %s", status, context.command, context.device_name, exc_info=True)


def failed(context: DeviceCommandContext, exc: BaseException, started: float, text: ErrorText) -> DeviceCommandContext:
    return context.model_copy(update={
        "outcome": instrument_outcome_of(exc).value,
        "error": text(exc),
        "error_type": type(exc).__name__,
        "duration_ms": (time.monotonic() - started) * 1000,
    })


@dataclass
class OneCommand:
    """One command as its sender names it, while it runs and until it is saved."""

    context: DeviceCommandContext
    events: IEventEmitter | None
    error_text: ErrorText
    # False when the sender cannot name its params as JSON, and the first wire command's are shown.
    params_named: bool = True
    started: float = field(default_factory=time.monotonic)
    sent: bool = False
    id_taken: bool = False
    # True while the pipeline runs the command this block names, so a step of it is not a second command.
    running: bool = False
    # True once a command sent inside this one reported that it got no answer, so this one files no second incident.
    inner_reported_no_answer: bool = False
    reported_no_answer: bool = False

    def wire_id(self) -> str:
        """The block's id for the first command it sends to a device, so the driver's events carry it; a fresh one after."""
        if self.id_taken:
            return str(uuid.uuid4())
        self.id_taken = True
        return self.context.command_id

    def reached(
        self, events: IEventEmitter, mode: str, params: Mapping[str, JsonValue], sim_speedup: float | None,
        nominal_seconds: float | None,
    ) -> None:
        """The device took the command: send STARTED, once, with the run's speed-up and the command's budgeted seconds."""
        if self.sent:
            return
        self.sent = True
        self.events = events
        update: dict[str, JsonValue] = {"mode": mode, "sim_speedup": sim_speedup, "nominal_seconds": nominal_seconds}
        if not self.params_named:
            update["params"] = dict(params)
        self.context = self.context.model_copy(update=update)
        emit_command(events, self.context, "STARTED")

    def answered(self, result: JsonValue) -> None:
        self.context = self.context.model_copy(update={"result": result})

    def names(self, device_name: str, command: str) -> bool:
        return self.context.device_name == device_name and self.context.command == command


_current: ContextVar[OneCommand | None] = ContextVar("current_device_command", default=None)


def params_as_sent(request: BaseModel) -> dict[str, JsonValue]:
    """A request's params as the wire carries them, so every sender's event names the same params."""
    return request.model_dump(mode="json", exclude_none=True)


def current_command() -> OneCommand | None:
    """The command a sender is running in this context, if one is."""
    return _current.get()


@asynccontextmanager
async def one_command(
    device_name: str, command: str, params: Mapping[str, JsonValue] | None, *, error_text: ErrorText,
    execution_id: str | None = None, events: IEventEmitter | None = None,
) -> AsyncIterator[OneCommand]:
    """Run one command and its records, and send its end event after them.

    `params` None takes the first wire command's. `events` sends a refusal that no device took as FAILED alone.
    Without it a command no device took sends nothing.
    """
    thread = current_thread.get()
    block = OneCommand(DeviceCommandContext(
        command_id=str(uuid.uuid4()), device_name=device_name, command=command,
        params=dict(params or {}), execution_id=execution_id,
        thread_id=thread.id if thread else None, thread_name=thread.name if thread else None,
    ), events, error_text, params_named=params is not None)
    token = _current.set(block)
    try:
        yield block
    except BaseException as exc:
        if block.events is not None:
            emit_command(block.events, failed(block.context, exc, block.started, block.error_text), "FAILED")
        raise
    finally:
        _current.reset(token)
    if block.events is not None and block.sent:
        done = block.context.model_copy(update={"duration_ms": (time.monotonic() - block.started) * 1000})
        emit_command(block.events, done, "COMPLETED")
