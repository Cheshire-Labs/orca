"""Device controller for command execution and locking.

The DeviceController manages command execution on devices, ensuring
exclusive access through a tracking dictionary and handling command
routing to device-bridge instances.

A disconnect-policy state machine layers over the command flow: when a
device disconnects mid-command, the future is held and a disconnect
timer is started. A reconnect, or the timer running out, fails the future
with DeviceOfflineError. No command is ever sent a second time.
The timer runs for the injected disconnect grace, the same for every device.

Recoverable timeouts are owned by the engine, not the controller. A
workflow command (one carrying an ``execution_id``) is bounded by the
engine's ``RecoverableTimeoutCoordinator``; the controller arms its own
``command_timer`` only for ad-hoc (no-execution_id) commands, where an
overrun fails the future and sends a wire ``CancelMessage``. When the
engine aborts or marks-complete a workflow command, the awaiting dispatch
inside ``execute_command`` is cancelled; the controller catches the
``CancelledError`` and sends a ``CancelMessage`` so the device bridge drops
the in-flight command and a late/orphan response can't resolve a future
the engine already settled.
"""

import uuid
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Set

from orca.gateway.controller.command_events import current_command
from orca.events.emitter import IEventEmitter
from orca.modes.run_modes import WorkflowRunMode
from orca.gateway.step_pacer_adapter import wire_budget
from orca.modes.sim_pacing import current_sim_pacer

from orca.runtime.danger import DangerLevel, write_audit
from cheshire_drivers.driver_errors import InstrumentOutcome
from orca.gateway.device_fault import (
    DeviceFault,
    DeviceFaultOutcome,
    fault_from_error,
)
from orca.gateway.controller.command_kind import CommandKind
from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    ConfirmationRequiredError,
    DeviceError,
    DeviceFaultedError,
    DeviceNotBroughtUpError,
    DeviceLockedError,
    DeviceOfflineError,
    CommandTimeoutError,
    InvalidCommandError,
    instrument_outcome_of,
)
from cheshire_drivers.command_table import COMMANDS
from pydantic import JsonValue, ValidationError as _PydanticValidationError

from orca.gateway.controller.command_clock import ICommandClock
from orca.resource_models.reach_in import ARM_REACH_IN_COMMANDS, mover_reaching_in
from orca.gateway.controller.disconnect_grace import IDisconnectGrace
from orca.gateway.registry.capabilities import validate_capability_for_device
from orca.gateway.registry import DeviceSnapshot, IDeviceConnectionTracker
from orca.gateway.websocket.manager import IClientChannel
from cheshire_drivers.gateway_protocol import CancelMessage, CommandMessage, ResponseMessage, MessageEnvelope

logger = logging.getLogger(__name__)

InterfaceSource = Callable[[str], Awaitable[Optional[frozenset[str]]]]
"""A device's name to the interfaces its commands are checked against; None when it knows no such device."""

def _outcome_of(error: BaseException) -> DeviceFaultOutcome:
    """What state to record the device in, once a fault is owed.

    Two, because a fault is only latched for a command that touched the
    instrument: it stopped part-way, or nothing answered and it may still be
    moving. A cancel is the second, which is why it reads through here rather
    than naming an outcome of its own.
    """
    if instrument_outcome_of(error) is InstrumentOutcome.UNKNOWN:
        return DeviceFaultOutcome.UNKNOWN
    return DeviceFaultOutcome.FAILED


Taken = Callable[[float | None, float | None], None]
"""Told when a device takes a command: the run's speed-up and the seconds budgeted for the command."""


def _moved_nothing(error: BaseException) -> bool:
    """Nothing on the instrument was touched, so no fault is owed.

    A refusal, and a command the device bridge could not act on at all: a
    device it does not hold, a command it does not have, a payload that will
    not deserialize.
    """
    return instrument_outcome_of(error).moved_nothing


COMMANDS_THAT_PUT_A_DEVICE_RIGHT = frozenset({"initialize", "home"})
"""The two commands an operator runs to put a machine into a known state.

Finishing one cleanly clears the device's fault. Every other command coming
back clean proves the device answers and nothing more.
"""


@dataclass
class _PendingCommand:
    """In-flight command awaiting response, including disconnect-recovery state.

    Stored in :attr:`DeviceController._pending` keyed by device_id so the
    connection-event listeners can find the right command on disconnect /
    reconnect. ``command_timer`` and ``disconnect_timer`` are mutually
    exclusive: only one is active at a time. ``command_timer`` runs
    while the device is connected; on disconnect it's cancelled and
    ``disconnect_timer`` takes its place. A reconnect fails the command.

    ``execution_id`` is the ad-hoc-vs-workflow discriminator: set when the
    caller is a workflow thread (the engine's recoverable-timeout
    coordinator bounds the command, so the controller arms NO command_timer
    for it), None for ad-hoc commands (the controller arms a fail-fast
    command_timer).
    """

    command_id: str
    device_id: str
    command: str
    timeout_seconds: float
    future: "asyncio.Future[JsonValue]"
    command_timer: Optional["asyncio.Task[None]"] = None
    disconnect_timer: Optional["asyncio.Task[None]"] = None
    execution_id: Optional[str] = None


class IDeviceController(Protocol):
    """Runs commands on connected devices, one at a time per device, and latches their faults."""

    @property
    def command_events(self) -> IEventEmitter:
        """Where each command's COMMAND events go."""
        ...

    def set_fault_listener(
        self, listener: Optional[Callable[[str, Optional[DeviceFault]], None]],
    ) -> None: ...

    def check_interfaces_with(self, source: InterfaceSource) -> None: ...

    def clear_fault_listener(
        self, listener: Callable[[str, Optional[DeviceFault]], None],
    ) -> None: ...

    def fault(self, device_id: str) -> Optional[DeviceFault]: ...

    async def clear_fault(self, device_id: str) -> Optional[DeviceFault]: ...

    def bring_up_error(self, device_id: str) -> Optional[str]: ...

    def mark_not_brought_up(self, device_id: str, error: str) -> None: ...

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Optional[Dict[str, JsonValue]] = None,
        timeout_seconds: Optional[float] = None,
        *,
        effective_mode: WorkflowRunMode,
        effective_interfaces: Optional[frozenset[str]] = None,
        kind: CommandKind = CommandKind.ACTUATION,
        execution_id: Optional[str] = None,
        confirm: bool = False,
    ) -> JsonValue: ...

    async def on_device_disconnected(self, device_id: str) -> None: ...

    async def on_device_reconnected(self, device_id: str) -> None: ...

    async def handle_response(self, response_msg: ResponseMessage) -> None: ...

    async def await_cancels_sent(self) -> None: ...


def _nothing(sim_speedup: float | None, nominal_seconds: float | None) -> None:
    return None


def _why_offline(tracker: IDeviceConnectionTracker, device_id: str) -> str:
    build_error = tracker.build_error(device_id)
    if build_error is None:
        return f"Device {device_id} disconnected"
    return f"Device {device_id} is offline: its orca-client could not build it: {build_error}"


class DeviceController:
    """
    Controls device command execution with exclusive locking.

    Uses a tracking dictionary to ensure only one command executes on
    a device at a time. Provides atomic check-and-set for device reservation.
    """

    def __init__(
        self,
        tracker: IDeviceConnectionTracker,
        connections: IClientChannel,
        disconnect_grace: IDisconnectGrace,
        command_clock: ICommandClock,
        *,
        events: IEventEmitter,
        serves_check_runs: bool = False,
        default_sim_speedup: float | None = None,
    ) -> None:
        self._tracker = tracker
        # What a simulated device waits at when no run paces its command; None waits for nothing.
        self._default_sim_speedup = default_sim_speedup
        self._events = events
        # Only a check run's own gateway, whose orca-client runs inside Orca, may be sent CHECK_RUN.
        self._serves_check_runs = serves_check_runs
        self._connections = connections
        self._disconnect_grace = disconnect_grace
        self._command_clock = command_clock
        # Single source of truth for "device is busy / which command owns
        # it": device_id -> in-flight record. Created at reservation (before
        # the wire dispatch) so a disconnect caught while the command is in
        # flight finds it. The connection-event listeners look it up here on
        # disconnect / reconnect.
        self._pending: Dict[str, _PendingCommand] = {}
        # command_id -> Future. Keyed by command_id because responses route
        # by it and world-sync ops register a future with no _pending record.
        self._command_futures: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()  # Protects all controller state below
        # device_id -> the command that did not come back clean, latched
        # until an operator clears it. See `device_fault`.
        self._faults: Dict[str, DeviceFault] = {}
        # device_id -> why a mode switch could not bring it up; cleared by a clean initialize.
        self._not_brought_up: Dict[str, str] = {}
        # None until the runtime wires itself up: the controller runs
        # standalone in tests and must not need an observer.
        self._fault_listener: Optional[
            Callable[[str, Optional[DeviceFault]], None]
        ] = None
        # Cancel sends in flight. They are scheduled rather than awaited, so
        # nothing else holds them: an unheld task can be collected mid-send.
        self._cancel_sends: Set[asyncio.Task[None]] = set()
        self._interfaces: InterfaceSource | None = None

    @property
    def command_events(self) -> IEventEmitter:
        return self._events

    def set_fault_listener(
        self, listener: Optional[Callable[[str, Optional[DeviceFault]], None]],
    ) -> None:
        """Watch faults arriving and clearing. Second argument is None on a clear."""
        self._fault_listener = listener

    def check_interfaces_with(self, source: InterfaceSource) -> None:
        """Where a command that names no interfaces gets the ones it is checked against: a run's, an engine's."""
        self._interfaces = source

    def clear_fault_listener(
        self, listener: Callable[[str, Optional[DeviceFault]], None],
    ) -> None:
        """Drop this listener, unless a newer runtime has already taken the slot.

        A rebuild can start the next runtime before the old one shuts down, and
        an unconditional clear there would silence the runtime that just began.
        """
        if self._fault_listener == listener:
            self._fault_listener = None

    def _tell_fault_listener(
        self, device_id: str, fault: Optional[DeviceFault],
    ) -> None:
        """Never let an observer's failure reach the command path."""
        if self._fault_listener is None:
            return
        try:
            self._fault_listener(device_id, fault)
        except Exception:
            logger.exception("device fault listener raised for %s", device_id)

    def fault(self, device_id: str) -> Optional[DeviceFault]:
        """The unresolved fault on this device, or None.

        Synchronous because status projections read it on every snapshot, and
        the value is immutable once latched.
        """
        return self._faults.get(device_id)

    async def clear_fault(self, device_id: str) -> Optional[DeviceFault]:
        """Give the device back to the workflow. Returns what was cleared.

        Most commands succeeding afterwards prove the machine answers, never
        that the plate it half-moved is where the ledger says, so the fault
        stands until someone says they looked. The exception is the pair in
        `COMMANDS_THAT_PUT_A_DEVICE_RIGHT`, which clear it themselves.
        """
        async with self._lock:
            cleared = self._faults.pop(device_id, None)
        if cleared is not None:
            logger.info(
                "Device %s fault cleared (was %s during %r)",
                device_id, cleared.outcome.value, cleared.command,
            )
            self._tell_fault_listener(device_id, None)
        return cleared

    async def _latch_fault(
        self,
        device_id: str,
        command: str,
        command_id: str,
        error: BaseException,
        outcome: DeviceFaultOutcome,
        execution_id: Optional[str],
    ) -> None:
        """Record that this command left the device in a state nobody has checked.

        The FIRST fault is the one kept. What follows it is usually the same
        trouble seen again from the next caller, and overwriting would rename
        the record off the command that actually stopped the machine.
        """
        async with self._lock:
            if device_id in self._faults:
                return
            self._faults[device_id] = fault_from_error(
                device_id=device_id,
                command=command,
                command_id=command_id,
                error=error,
                outcome=outcome,
                execution_id=execution_id,
            )
            latched = self._faults[device_id]
        # The raiser carries the fault it left, so a caller holding only the
        # exception can name it later without matching on a device name.
        if isinstance(error, DeviceError):
            error.device_fault = latched
        logger.error("Device %s faulted: %s", device_id, latched.describe())
        self._tell_fault_listener(device_id, latched)

    def bring_up_error(self, device_id: str) -> Optional[str]:
        """Why the device did not come up after a switch between sim and live, or None."""
        return self._not_brought_up.get(device_id)

    def mark_not_brought_up(self, device_id: str, error: str) -> None:
        """Refuse operator commands to the device until a clean initialize."""
        self._not_brought_up[device_id] = error

    async def _refuse_if_faulted(self, device_id: str) -> None:
        """Keep the engine off a device whose last command left it unchecked."""
        fault = self._faults.get(device_id)
        if fault is None:
            return
        raise DeviceFaultedError(fault.describe(), fault)

    async def _clear_fault_if_put_right(
        self, device_id: str, command: str, kind: CommandKind,
    ) -> None:
        """A clean bring-up or home is the operator saying they dealt with it.

        The membership check is what makes it an operator's command, not the
        refusal upstream: a world-sync op is never refused on a faulted device,
        so it reaches here with the fault standing and the engine, not a
        person, behind it.
        """
        if not kind.occupies_the_device:
            return
        if command not in COMMANDS_THAT_PUT_A_DEVICE_RIGHT:
            return
        cleared = await self.clear_fault(device_id)
        if cleared is not None:
            logger.info(
                "Device %s fault cleared by a clean %r", device_id, command,
            )

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Optional[Dict[str, JsonValue]] = None,
        timeout_seconds: Optional[float] = None,
        *,
        effective_mode: WorkflowRunMode,
        effective_interfaces: Optional[frozenset[str]] = None,
        kind: CommandKind = CommandKind.ACTUATION,
        execution_id: Optional[str] = None,
        confirm: bool = False,
    ) -> JsonValue:
        """Execute a command on a device.

        The sender's `one_command` block sends the command's COMMAND events: STARTED once the device is reserved
        for it, and the end one after its records. A command outside a block sends none.
        """
        async def run(command_id: str, taken: Taken) -> JsonValue:
            return await self._execute_command(
                command_id, device_id, command, params, timeout_seconds,
                effective_mode=effective_mode, effective_interfaces=effective_interfaces,
                kind=kind,
                execution_id=execution_id, confirm=confirm, taken=taken,
            )

        block = current_command()
        if block is None:
            return await run(str(uuid.uuid4()), _nothing)
        if block.events is None:
            block.events = self._events
        result = await run(
            block.wire_id(),
            lambda sim_speedup, nominal_seconds: block.reached(
                self._events, effective_mode.value, params or {}, sim_speedup, nominal_seconds,
            ),
        )
        block.answered(result)
        return result

    async def _execute_command(
        self,
        command_id: str,
        device_id: str,
        command: str,
        params: Optional[Dict[str, JsonValue]],
        timeout_seconds: Optional[float],
        *,
        effective_mode: WorkflowRunMode,
        effective_interfaces: Optional[frozenset[str]],
        kind: CommandKind,
        execution_id: Optional[str],
        confirm: bool,
        taken: Taken,
    ) -> JsonValue:
        """Execute a command on a device.

        ``confirm`` is the caller's acknowledgement for a vendor command the
        driver's catalog flags ``requires_confirm`` (anything not declared
        read-safe). Without it such a command raises
        :class:`ConfirmationRequiredError` before any wire send. Interface
        contract commands and read-safe extras ignore it.

        Phases: validate -> reserve -> dispatch -> register-pending ->
        await -> release. Each phase is a private helper; this method is
        the orchestrator. Behavior matches the previous single-body form.

        ``kind`` says whether this command takes the device. A
        ``WORLD_SYNC`` op does not: it skips the busy-check, the ``_pending``
        record, the command timer and the faulted-device refusal, and it can
        leave nothing mid-motion to fault the device for. Only the
        ``_command_futures`` entry, which routes the response, is kept. See
        :class:`CommandKind`.

        ``effective_mode`` is what the device runs as for this command: the run
        mode, or under LIVE the device's own sim or live setting. Inlined into the wire ``CommandMessage`` so the
        device bridge can pick between LIVE and SIM. CHECK_RUN is refused
        unless this controller serves a check run's own gateway.

        ``effective_interfaces`` is what the command is checked against. When
        None, the source set by ``check_interfaces_with`` answers, so every
        sender gets the same check. When that has no answer either, the
        connection's advertised set is used.

        Raises:
            DeviceOfflineError: Device not connected.
            InvalidCommandError: Command not supported by device type, or
                CHECK_RUN sent to a gateway that serves no check run.
            DeviceLockedError: Device is busy with another command.
            CommandTimeoutError: No response within timeout.
        """
        if params is None:
            params = {}

        # Engine work only. An operator's own commands are how a faulted
        # device gets looked at, so they are never refused here.
        if execution_id is not None and kind.occupies_the_device:
            await self._refuse_if_faulted(device_id)
        not_brought_up = self._not_brought_up.get(device_id)
        if not_brought_up is not None and kind.occupies_the_device and command != "initialize":
            raise DeviceNotBroughtUpError(not_brought_up)

        device = await self._validate_command(
            device_id, command, params, effective_mode, effective_interfaces,
            confirm,
        )

        if timeout_seconds is None:
            timeout_seconds = self._command_clock.timeout_for(device, command, params)

        method_info = device.methods.get(command)
        flagged_dangerous = method_info is not None and method_info.requires_confirm

        command_future: asyncio.Future[JsonValue] = asyncio.Future()
        pacer = current_sim_pacer.get()
        # A device set to sim is paced by its simulator whoever sent the command, a LIVE run included.
        # A live device is never paced.
        sim_speedup = None
        if effective_mode is WorkflowRunMode.SIM:
            sim_speedup = self._default_sim_speedup if pacer is None else pacer.speedup
        # Sent on every command: a sim the device bridge built stands in for hardware and spends it even on LIVE.
        nominal_seconds = wire_budget(effective_interfaces or frozenset(device.interfaces), command, params)

        await self._reserve_device(
            command_id=command_id,
            device_id=device_id,
            command=command,
            timeout_seconds=timeout_seconds,
            command_future=command_future,
            execution_id=execution_id,
            kind=kind,
        )
        taken(sim_speedup, nominal_seconds)
        # Nothing has reached the machine until the dispatch returns, so a
        # failure before that point leaves no fault behind.
        dispatched = False
        try:
            await self._dispatch_command(
                device_id, command_id, command, params, effective_mode,
                sim_speedup, nominal_seconds,
            )
            dispatched = True
            # The send is the auditable fact for hardware: a command that
            # later times out may still have moved the device.
            if flagged_dangerous:
                write_audit(
                    f"device.{command}",
                    DangerLevel.PHYSICAL,
                    None,
                    {"device_id": device_id, "params": params},
                )
            # Only ad-hoc commands get a gateway fail-fast timer; workflow
            # commands (execution_id set) are bounded by the engine's
            # recoverable-timeout coordinator.
            if kind.occupies_the_device and execution_id is None:
                await self._arm_command_timer(device_id)
            result = await self._await_command_result(
                command_future, command_id, device_id, timeout_seconds,
                kind,
            )
            await self._clear_fault_if_put_right(device_id, command, kind)
            if command == "initialize":
                self._not_brought_up.pop(device_id, None)
            return result
        except asyncio.CancelledError as exc:
            # Scheduled (not awaited): the cancel must not block the unwind.
            # It goes first because _latch_fault takes the controller lock that
            # every reserve and release also takes, and an abort unwinding many
            # threads is when that queue is longest. The cancel is what stops a
            # late orphan response resolving a future the engine has settled.
            self._send_cancel_detached(device_id, command_id)
            # The cancel stops the device bridge's own task and reaches no
            # further, so the motion it started is still running.
            if dispatched and kind.occupies_the_device:
                await self._latch_fault(
                    device_id, command, command_id, exc,
                    _outcome_of(exc), execution_id,
                )
            raise
        except DeviceError as exc:
            # Nothing to fault a device for when nothing on it moved, and
            # latching anyway stops work that was fine.
            if (
                dispatched
                and kind.occupies_the_device
                and not _moved_nothing(exc)
            ):
                exc.may_have_moved = True
                await self._latch_fault(
                    device_id, command, command_id, exc,
                    _outcome_of(exc), execution_id,
                )
            raise
        finally:
            popped = await self._release_device(
                command_id, device_id, kind,
            )
            if kind.occupies_the_device and popped is not None:
                self._cancel_timers(popped)

    async def _validate_command(
        self,
        device_id: str,
        command: str,
        params: Dict[str, JsonValue],
        effective_mode: WorkflowRunMode,
        effective_interfaces: Optional[frozenset[str]],
        confirm: bool,
    ) -> DeviceSnapshot:
        """Pre-flight validation: mode, presence, capability, confirm, param shape.

        Mutates no controller state. Raises InvalidCommandError /
        DeviceOfflineError exactly as the inline checks did. Returns the
        resolved device snapshot (the caller needs it for timeout lookup).
        """
        if effective_mode is WorkflowRunMode.CHECK_RUN and not self._serves_check_runs:
            raise InvalidCommandError(
                f"{command!r} on {device_id!r} is a check-run command, and a check run runs on its own "
                f"gateway inside Orca, never on a lab PC."
            )
        if command in ARM_REACH_IN_COMMANDS and mover_reaching_in() is None:
            raise InvalidCommandError(
                f"{command!r} on {device_id!r} reaches into a device, so it is sent "
                f"only through a transporter's reach-in, which opens the device and "
                f"holds it while the arm is inside."
            )

        device = await self._tracker.get_device(device_id)
        if not device:
            raise DeviceOfflineError(f"Device {device_id} not found or offline")

        if effective_interfaces is None and self._interfaces is not None:
            effective_interfaces = await self._interfaces(device_id)
        if effective_interfaces is None:
            interfaces_advertised = frozenset(device.interfaces)
        else:
            interfaces_advertised = effective_interfaces
        capabilities_advertised = frozenset(device.capabilities)
        if not validate_capability_for_device(
            interfaces_advertised, capabilities_advertised, command
        ):
            raise InvalidCommandError(
                f"Device {device_id!r} (type {device.type!r}) does not "
                f"support command {command!r}"
            )

        method_info = device.methods.get(command)
        if method_info is not None and method_info.requires_confirm and not confirm:
            raise ConfirmationRequiredError(
                f"Command {command!r} on device {device_id!r} is a vendor "
                f"command not declared read-safe; re-send with confirm=true "
                f"after a human has acknowledged it"
            )

        self._validate_params(command, params, interfaces_advertised, device_id)
        return device

    def _validate_params(
        self,
        command: str,
        params: Dict[str, JsonValue],
        interfaces_advertised: frozenset[str],
        device_id: str,
    ) -> None:
        """Refuse params that do not fit the command's request model, before the WebSocket send.

        A vendor extra has no model, so its params go through unchecked.
        """
        try:
            found = COMMANDS.find(interfaces_advertised, command)
            if found is not None:
                found.validate(params)
        except (ValueError, _PydanticValidationError) as e:
            raise InvalidCommandError(
                f"Invalid params for {command!r} on device {device_id!r}: {e}"
            ) from e

    async def _reserve_device(
        self,
        command_id: str,
        device_id: str,
        command: str,
        timeout_seconds: float,
        command_future: "asyncio.Future[JsonValue]",
        execution_id: Optional[str],
        kind: CommandKind,
    ) -> None:
        """Atomically reserve the device, unless this is a world-sync op.

        Creates the ``_pending`` record (with both timers unset) the instant
        the device is reserved, BEFORE the wire dispatch -- so a disconnect
        caught while the command is in flight always finds it. The
        command-timer is armed later, in ``_arm_command_timer`` after a
        successful dispatch.

        World-sync ops skip the busy-check + ``_pending`` record so they
        never raise ``DeviceLockedError`` against (or block) a concurrent
        workflow command on the same device; their future is still
        registered so the wire response routes back.
        """
        async with self._lock:
            if kind.occupies_the_device:
                existing = self._pending.get(device_id)
                if existing is not None:
                    raise DeviceLockedError(
                        f"Device {device_id} is busy executing command "
                        f"{existing.command_id}"
                    )
                self._pending[device_id] = _PendingCommand(
                    command_id=command_id,
                    device_id=device_id,
                    command=command,
                    timeout_seconds=timeout_seconds,
                    future=command_future,
                    execution_id=execution_id,
                )
            self._command_futures[command_id] = command_future

    async def _dispatch_command(
        self,
        device_id: str,
        command_id: str,
        command: str,
        params: Dict[str, JsonValue],
        effective_mode: WorkflowRunMode,
        sim_speedup: Optional[float],
        nominal_seconds: Optional[float],
    ) -> None:
        """Re-check online, resolve the client, send the command envelope.

        Raises ``DeviceOfflineError`` if the device dropped between
        reservation and send, has no client, or the wire send fails.
        """
        if not await self._tracker.is_device_online(device_id):
            raise DeviceOfflineError(_why_offline(self._tracker, device_id))

        client_id = await self._tracker.get_client_for_device(device_id)
        if client_id is None:
            raise DeviceOfflineError(f"Device {device_id} has no connected client")

        command_msg = CommandMessage(
            command_id=command_id,
            device_name=device_id,
            command=command,
            params=params,
            effective_mode=effective_mode.value,
            sim_speedup=sim_speedup,
            nominal_seconds=nominal_seconds,
        )
        envelope = MessageEnvelope.wrap_command(command_msg)
        success = await self._connections.send_to_client(
            client_id, envelope.model_dump_json()
        )
        if not success:
            raise DeviceOfflineError(
                f"Failed to send command to client {client_id}"
            )
        logger.info(
            f"Sent command {command_id} ({command}) to device {device_id} via client {client_id}"
        )

    async def _arm_command_timer(self, device_id: str) -> None:
        """Arm the command timer after a successful dispatch.

        Skips arming if a disconnect already swapped in a ``disconnect_timer``
        while the dispatch was in flight (the disconnect path owns the
        lifecycle now), or if the record/future is already gone/done -- so
        the two timers are never both live. Done under the lock so the
        ``disconnect_timer is None`` check and the arm are atomic against a
        concurrent disconnect listener.
        """
        async with self._lock:
            pending = self._pending.get(device_id)
            if pending is None or pending.future.done():
                return
            if pending.disconnect_timer is not None:
                return
            pending.command_timer = asyncio.create_task(
                self._command_timeout_watcher(pending),
            )

    async def _await_command_result(
        self,
        command_future: "asyncio.Future[JsonValue]",
        command_id: str,
        device_id: str,
        timeout_seconds: float,
        kind: CommandKind,
    ) -> JsonValue:
        """Await the wire response and translate timeouts.

        Workflow commands await unbounded; the ``command_timer`` task is
        what fails the future on timeout, giving the disconnect listener
        room to swap timers without racing a ``wait_for`` window. World-sync
        ops have no timer, so they fall back to a bounded ``wait_for`` whose
        ``asyncio.TimeoutError`` is remapped to ``CommandTimeoutError``.
        """
        try:
            if not kind.occupies_the_device:
                return await asyncio.wait_for(
                    command_future, timeout=timeout_seconds,
                )
            return await command_future
        except asyncio.TimeoutError as e:
            logger.error(
                f"World-sync command {command_id} timed out after {timeout_seconds}s"
            )
            raise CommandTimeoutError(
                f"World-sync command {command_id} on device {device_id} "
                f"timed out after {timeout_seconds}s"
            ) from e
        except CommandTimeoutError:
            logger.error(
                f"Command {command_id} timed out after {timeout_seconds}s"
            )
            raise

    async def _release_device(
        self,
        command_id: str,
        device_id: str,
        kind: CommandKind,
    ) -> Optional[_PendingCommand]:
        """Release device state in the finally path; return the popped pending.

        ``_command_futures`` is always cleaned up (keyed by the unique
        command_id). Only this command's own record is popped, so a world-sync
        op, or a command whose record a reconnect already dropped, never takes
        another command's record for the same device.
        """
        async with self._lock:
            self._command_futures.pop(command_id, None)
            if not kind.occupies_the_device:
                return None
            pending = self._pending.get(device_id)
            if pending is None or pending.command_id != command_id:
                return None
            return self._pending.pop(device_id)

    async def _command_timeout_watcher(self, pending: _PendingCommand) -> None:
        """Fail an ad-hoc command that overran its timeout + cancel the wire.

        Armed only for ad-hoc (no-execution_id) commands; workflow commands
        are bounded by the engine's recoverable-timeout coordinator. On
        expiry, fail the future and send a ``CancelMessage`` so the device
        bridge drops the in-flight command and a late response can't resolve
        a settled future.

        Cancelled by the disconnect listener while the device is offline.
        """
        try:
            await asyncio.sleep(pending.timeout_seconds)
        except asyncio.CancelledError:
            return
        if pending.future.done():
            return

        logger.error(
            "Command %s on device %s timed out after %ss",
            pending.command_id, pending.device_id, pending.timeout_seconds,
        )
        pending.future.set_exception(
            CommandTimeoutError(
                f"Command {pending.command_id} on device "
                f"{pending.device_id} timed out after "
                f"{pending.timeout_seconds}s"
            )
        )
        self._send_cancel_detached(pending.device_id, pending.command_id)

    async def _disconnect_timeout_watcher(
        self, pending: _PendingCommand, timeout: float,
    ) -> None:
        """Fire DeviceOfflineError on the future after the disconnect grace.

        Started by the disconnect listener when a device drops mid-command;
        cancelled by the reconnect listener if the device returns within
        the grace window.
        """
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        if not pending.future.done():
            logger.error(
                "Device %s did not reconnect within %ss; failing command %s",
                pending.device_id, timeout, pending.command_id,
            )
            pending.future.set_exception(
                DeviceOfflineError(
                    f"Device {pending.device_id} did not reconnect within "
                    f"{timeout}s; command {pending.command_id} aborted"
                )
            )

    def _cancel_timers(self, pending: _PendingCommand) -> None:
        """Cancel both timers for a pending command. Safe to call repeatedly."""
        for timer in (pending.command_timer, pending.disconnect_timer):
            if timer is not None and not timer.done():
                timer.cancel()
        pending.command_timer = None
        pending.disconnect_timer = None

    async def on_device_disconnected(self, device_id: str) -> None:
        """Hold any in-flight command for this device, start the disconnect timer.

        Connection-event listener callable. Idempotent: a duplicate
        disconnect signal for an already-disconnected device is a no-op.
        Subscribed by ``orca.gateway.connection_handling``.
        """
        async with self._lock:
            pending = self._pending.get(device_id)
            if pending is None or pending.future.done():
                return
            # Already disconnected? Re-arm the disconnect timer is OK,
            # but don't double-cancel a non-existent command_timer.
            if pending.disconnect_timer is not None:
                return
            cmd_timer = pending.command_timer
            pending.command_timer = None
            if cmd_timer is not None and not cmd_timer.done():
                cmd_timer.cancel()
            timeout = self._disconnect_grace.seconds
            pending.disconnect_timer = asyncio.create_task(
                self._disconnect_timeout_watcher(pending, timeout),
            )
        logger.info(
            "Device %s disconnected mid-command %s; holding for %ss",
            device_id, pending.command_id, timeout,
        )

    async def on_device_reconnected(self, device_id: str) -> None:
        """Fail a command that was in flight when the device dropped, now that it is back.

        No command is ever sent a second time: the device may have run it, and
        only a person can say. A reconnect with nothing held is a no-op.
        Subscribed by ``orca.gateway.connection_handling``.
        """
        async with self._lock:
            pending = self._pending.get(device_id)
            if pending is None or pending.future.done():
                return
            if pending.disconnect_timer is None:
                # No disconnect came first: a first connect, nothing was held.
                return
            disconnect_timer = pending.disconnect_timer
            pending.disconnect_timer = None
            self._pending.pop(device_id, None)
        if not disconnect_timer.done():
            disconnect_timer.cancel()
        logger.warning(
            "Device %s reconnected mid-command %s (%s); the command is not sent again. "
            "Manual recovery required.",
            device_id, pending.command_id, pending.command,
        )
        pending.future.set_exception(
            DeviceOfflineError(
                f"Command lost during reconnect; manual recovery "
                f"required (device={device_id}, "
                f"command_id={pending.command_id}, "
                f"command={pending.command})"
            )
        )

    async def handle_response(self, response_msg: ResponseMessage) -> None:
        """
        Handle a response from the device bridge.

        Called by the WebSocket router when a ResponseMessage is received.
        Completes the corresponding Future to unblock execute_command().
        A response for an unknown command_id (already settled by a timeout,
        cancel, or emergency unlock) is logged and ignored.

        Args:
            response_msg: ResponseMessage from the device bridge
        """
        command_id = response_msg.command_id

        async with self._lock:
            future = self._command_futures.get(command_id)

        if future is None or future.done():
            logger.warning(
                f"Received response for unknown command {command_id} (may have timed out)"
            )
            return

        # Set result or exception on future
        if response_msg.success:
            future.set_result(response_msg.result)
            logger.debug(f"Command {command_id} completed successfully")
        else:
            # Create exception from error response - use CommandExecutionError
            # so callers can distinguish driver errors from internal errors
            error_msg = response_msg.error or "Unknown error"
            error_type = response_msg.error_type or "CommandError"
            future.set_exception(CommandExecutionError(
                error_msg, error_type, response_msg.instrument_outcome,
            ))
            logger.warning(f"Command {command_id} failed: {error_msg}")

    def _send_cancel_detached(self, device_id: str, command_id: str) -> None:
        """Schedule the cancel without waiting for it, and keep the task.

        Callers reach here while unwinding and must not block on the wire. The
        task is held so it survives to be sent and so shutdown can wait for it.
        """
        task = asyncio.create_task(self._send_cancel(device_id, command_id))
        self._cancel_sends.add(task)
        task.add_done_callback(self._cancel_send_finished)

    def _cancel_send_finished(self, task: "asyncio.Task[None]") -> None:
        self._cancel_sends.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("Cancel send failed: %s", error, exc_info=error)

    async def await_cancels_sent(self) -> None:
        """Wait for every scheduled cancel to reach the wire, or fail trying.

        Re-reads rather than snapshotting: an execution torn down late in a
        shutdown schedules another cancel while this is already waiting.
        Only this loop's tasks are waited for, and one left behind by a loop
        that has since closed is dropped -- it can never finish or call back.
        """
        loop = asyncio.get_running_loop()
        while True:
            self._cancel_sends.difference_update({
                task for task in self._cancel_sends if task.get_loop().is_closed()
            })
            pending = [
                task for task in self._cancel_sends
                if task.get_loop() is loop and not task.done()
            ]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def _send_cancel(self, device_id: str, command_id: str) -> None:
        """Best-effort: tell the device bridge to drop an in-flight command.

        Sent on engine abort/mark-complete (the awaiting dispatch was
        cancelled) and on ad-hoc command-timer expiry, so a late orphan
        response can't resolve a future the controller already settled. A
        missing client or failed send is logged and swallowed -- the future
        is already settled either way.
        """
        client_id = await self._tracker.get_client_for_device(device_id)
        if client_id is None:
            logger.info(
                "No client for device %s; cannot send cancel for command %s",
                device_id, command_id,
            )
            return
        envelope = MessageEnvelope.wrap_cancel(
            CancelMessage(command_id=command_id, reason="engine_cancelled"),
        )
        success = await self._connections.send_to_client(
            client_id, envelope.model_dump_json(),
        )
        if not success:
            logger.warning(
                "Failed to send cancel for command %s to device %s",
                command_id, device_id,
            )
