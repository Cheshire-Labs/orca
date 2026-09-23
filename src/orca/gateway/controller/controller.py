"""Device controller for command execution and locking.

The DeviceController manages command execution on devices, ensuring
exclusive access through a tracking dictionary and handling command
routing to device-bridge instances.

A disconnect-policy state machine layers over the command flow: when a
device disconnects mid-command, the future is held (not failed), a
disconnect timer is started, and on reconnect within the timeout the
command is re-sent transparently. On timeout, the future fails with
DeviceOfflineError. Disconnect grace is resolved through
:class:`DisconnectGrace` -- topology-card override first, then a single
conservative fallback.

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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from orca.runtime.run_modes import WorkflowRunMode

from orca.runtime.danger import DangerLevel, audit_logger
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
    DeviceLockedError,
    DeviceOfflineError,
    CommandTimeoutError,
    InvalidCommandError,
    instrument_outcome_of,
)
from cheshire_drivers.centrifuge_request_validation import validate_centrifuge_payload
from cheshire_drivers.delidder_request_validation import validate_delidder_payload
from cheshire_drivers.lh_motion_request_validation import validate_lh_motion_payload
from cheshire_drivers.lh_request_validation import (
    validate_lh_payload,
    validate_liquid_probe_payload,
)
from cheshire_drivers.protocol_runner_request_validation import validate_protocol_runner_payload
from cheshire_drivers.reader_request_validation import validate_reader_payload
from cheshire_drivers.sealer_request_validation import validate_sealer_payload
from cheshire_drivers.shaker_request_validation import validate_shaker_payload
from cheshire_drivers.transporter_request_validation import validate_transporter_payload
from pydantic import JsonValue, ValidationError as _PydanticValidationError

from orca.gateway.controller.command_clock import DeviceCommandClock
from orca.gateway.controller.disconnect_grace import DisconnectGrace
from orca.gateway.registry.capabilities import validate_capability_for_device
from orca.gateway.registry import DeviceSnapshot, device_connection_tracker
from orca.gateway.websocket.manager import connection_manager
from cheshire_drivers.gateway_protocol import CancelMessage, CommandMessage, LabwareDefinitionDTO, ResponseMessage, MessageEnvelope

logger = logging.getLogger(__name__)

# The manual-control interfaces whose commands share one payload validator. A device advertising
# any of them gets its motion params checked against the motion request models.
_LH_MOTION_INTERFACES = frozenset(
    {
        "IPipetteMotion",
        "IGripperMotion",
        "IGripperPosition",
        "IForceGripperJaw",
        "IWidthGripperJaw",
        "IGripperRotation",
    }
)


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
    ``disconnect_timer`` takes its place. On reconnect, the reverse.

    ``resend_on_reconnect`` is the per-command opt-out for the default
    disconnect-recovery resend behavior. Drivers whose commands
    are non-idempotent (notably liquid handlers: aspirate / dispense /
    tip ops) pass ``False`` so the reconnect listener fails the future
    with ``DeviceOfflineError`` instead of double-executing on the
    physical device. Defaults to ``True`` to preserve the legacy
    transparent-resend behavior for safe drivers.

    ``execution_id`` is the ad-hoc-vs-workflow discriminator: set when the
    caller is a workflow thread (the engine's recoverable-timeout
    coordinator bounds the command, so the controller arms NO command_timer
    for it), None for ad-hoc commands (the controller arms a fail-fast
    command_timer).
    """

    command_id: str
    device_id: str
    command: str
    params: Dict[str, JsonValue]
    effective_mode: WorkflowRunMode
    timeout_seconds: float
    future: "asyncio.Future[JsonValue]"
    command_timer: Optional["asyncio.Task[None]"] = None
    disconnect_timer: Optional["asyncio.Task[None]"] = None
    resend_on_reconnect: bool = True
    labware: Optional[LabwareDefinitionDTO] = None
    execution_id: Optional[str] = None


class DeviceController:
    """
    Controls device command execution with exclusive locking.

    Uses a tracking dictionary to ensure only one command executes on
    a device at a time. Provides atomic check-and-set for device reservation.
    """

    def __init__(self):
        """Initialize the device controller."""
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
        # Disconnect-grace resolver. Defaults to a fresh DisconnectGrace
        # with no topology resolver; main.py wires the runtime topology
        # lookup once the system is built.
        self._disconnect_grace = DisconnectGrace()
        # Per-command timeout source. Reads driver-advertised
        # MethodInfo.duration; falls back to a single conservative default
        # when no duration was advertised.
        self._command_clock = DeviceCommandClock()
        # device_id -> the command that did not come back clean, latched
        # until an operator clears it. See `device_fault`.
        self._faults: Dict[str, DeviceFault] = {}
        # None until the runtime wires itself up: the controller runs
        # standalone in tests and must not need an observer.
        self._fault_listener: Optional[
            Callable[[str, Optional[DeviceFault]], None]
        ] = None
        # Cancel sends in flight. They are scheduled rather than awaited, so
        # nothing else holds them: an unheld task can be collected mid-send.
        self._cancel_sends: Set[asyncio.Task[None]] = set()

    def set_fault_listener(
        self, listener: Optional[Callable[[str, Optional[DeviceFault]], None]],
    ) -> None:
        """Watch faults arriving and clearing. Second argument is None on a clear."""
        self._fault_listener = listener

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

    def set_command_clock(self, clock: DeviceCommandClock) -> None:
        """Inject a custom DeviceCommandClock (tests + per-deployment overrides)."""
        self._command_clock = clock

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

    def set_disconnect_grace(self, grace: DisconnectGrace) -> None:
        """Inject the DisconnectGrace instance (tests + main.py wiring).

        Replaces the previous ``set_disconnect_timeout_resolver`` API:
        callers now construct a ``DisconnectGrace`` (optionally with a
        topology resolver) and inject it as a unit.
        """
        self._disconnect_grace = grace

    async def is_locked(self, device_id: str) -> bool:
        """
        Check if device is currently executing a command.

        This is a best-effort hint - the device might become locked
        between this check and subsequent operations. For guaranteed
        exclusive access, use execute_command() which performs atomic
        check-and-set.

        Args:
            device_id: Device to check

        Returns:
            True if device is currently locked, False otherwise
        """
        async with self._lock:
            return device_id in self._pending

    async def get_available_devices(
        self, device_type: Optional[str] = None
    ) -> List[DeviceSnapshot]:
        """Get list of devices that are online AND not locked."""
        all_devices = await device_connection_tracker.list_devices(
            device_type=device_type, status="ready"
        )

        # Filter out locked devices
        async with self._lock:
            available = [
                device
                for device in all_devices
                if device.name not in self._pending
            ]

        return available

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Optional[Dict[str, JsonValue]] = None,
        timeout_seconds: Optional[float] = None,
        *,
        effective_mode: WorkflowRunMode,
        effective_interfaces: Optional[frozenset[str]] = None,
        resend_on_reconnect: bool = True,
        kind: CommandKind = CommandKind.ACTUATION,
        labware: Optional[LabwareDefinitionDTO] = None,
        execution_id: Optional[str] = None,
        confirm: bool = False,
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

        ``effective_mode`` is the per-dispatch run-mode resolution from the
        orca-core hierarchy (deployment_base -> topology_sim_override ->
        submit_override). Inlined into the wire ``CommandMessage`` so the
        device bridge can pick between LIVE and DEVICE_SIM. PURE_SIM must
        never reach this method; an InvalidCommandError is raised before
        any wire send when it does.

        ``effective_interfaces`` is the dispatch contract resolved from the
        unified ``runtime.device_registry``. When provided it overrides the
        connection-tracker's advertised set so capability drift (advertised
        broader than topology declared) is blocked at the controller. When
        ``None`` (direct callers without runtime context), validation falls
        back to the connection tracker's advertised set.

        ``resend_on_reconnect`` controls the disconnect-recovery policy for
        THIS command. Default ``True`` preserves the transparent-resend
        behavior. Pass ``False`` for non-idempotent commands (e.g. LH
        aspirate / dispense / tip ops); on reconnect the controller fails
        the future with ``DeviceOfflineError`` instead of resending.

        Raises:
            DeviceOfflineError: Device not connected.
            InvalidCommandError: Command not supported by device type, or
                ``effective_mode`` is PURE_SIM.
            DeviceLockedError: Device is busy with another command.
            CommandTimeoutError: No response within timeout.
        """
        if params is None:
            params = {}

        # Engine work only. An operator's own commands are how a faulted
        # device gets looked at, so they are never refused here.
        if execution_id is not None and kind.occupies_the_device:
            await self._refuse_if_faulted(device_id)

        device = await self._validate_command(
            device_id, command, params, effective_mode, effective_interfaces,
            confirm,
        )

        if timeout_seconds is None:
            timeout_seconds = self._command_clock.timeout_for(device, command)

        method_info = device.methods.get(command)
        flagged_dangerous = method_info is not None and method_info.requires_confirm

        command_id = str(uuid.uuid4())
        command_future: asyncio.Future[JsonValue] = asyncio.Future()

        await self._reserve_device(
            command_id=command_id,
            device_id=device_id,
            command=command,
            params=params,
            effective_mode=effective_mode,
            timeout_seconds=timeout_seconds,
            command_future=command_future,
            # A flagged command has unknown idempotency and a silent resend
            # would dodge the audit; fail offline instead of re-driving hardware.
            resend_on_reconnect=resend_on_reconnect and not flagged_dangerous,
            labware=labware,
            execution_id=execution_id,
            kind=kind,
        )
        # Nothing has reached the machine until the dispatch returns, so a
        # failure before that point leaves no fault behind.
        dispatched = False
        try:
            await self._dispatch_command(
                device_id, command_id, command, params, effective_mode, labware,
            )
            dispatched = True
            # The send is the auditable fact for hardware: a command that
            # later times out may still have moved the device.
            if flagged_dangerous:
                audit_logger.info(
                    "%s level=%s reason=%r args=%r",
                    f"device.{command}",
                    DangerLevel.PHYSICAL.name,
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
        if effective_mode is WorkflowRunMode.PURE_SIM:
            raise InvalidCommandError(
                f"PURE_SIM commands must not reach the device gateway "
                f"(device_id={device_id!r}, command={command!r})"
            )

        device = await device_connection_tracker.get_device(device_id)
        if not device:
            raise DeviceOfflineError(f"Device {device_id} not found or offline")

        # When the gateway resolved an entry in the unified registry it
        # passes ``effective_interfaces`` = topology-declared INTERSECT
        # advertised, which blocks capability drift. ``None`` (direct
        # callers without runtime context) falls back to advertised.
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
        """Validate ``params`` shape at the wire boundary, gated per interface.

        Catches bad params server-side before the WebSocket send. Each
        validator is gated on the device's advertised interface so a
        cross-category command name never validates against the wrong
        payload model.
        """
        try:
            if "ILiquidHandler" in interfaces_advertised:
                validate_lh_payload(command, params)
            if "ILiquidProbe" in interfaces_advertised:
                validate_liquid_probe_payload(command, params)
            if interfaces_advertised & _LH_MOTION_INTERFACES:
                validate_lh_motion_payload(command, params)
            if "ITransporter" in interfaces_advertised:
                validate_transporter_payload(command, params)
            if "IShaker" in interfaces_advertised:
                validate_shaker_payload(command, params)
            if "IDelidder" in interfaces_advertised:
                validate_delidder_payload(command, params)
            if "ISealer" in interfaces_advertised:
                validate_sealer_payload(command, params)
            if "ICentrifuge" in interfaces_advertised:
                validate_centrifuge_payload(command, params)
            if "IReader" in interfaces_advertised:
                validate_reader_payload(command, params)
            if "IProtocolRunner" in interfaces_advertised:
                validate_protocol_runner_payload(command, params)
        except (ValueError, _PydanticValidationError) as e:
            raise InvalidCommandError(
                f"Invalid params for {command!r} on device {device_id!r}: {e}"
            ) from e

    async def _reserve_device(
        self,
        command_id: str,
        device_id: str,
        command: str,
        params: Dict[str, JsonValue],
        effective_mode: WorkflowRunMode,
        timeout_seconds: float,
        command_future: "asyncio.Future[JsonValue]",
        resend_on_reconnect: bool,
        labware: Optional[LabwareDefinitionDTO],
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
                    params=params,
                    effective_mode=effective_mode,
                    timeout_seconds=timeout_seconds,
                    future=command_future,
                    resend_on_reconnect=resend_on_reconnect,
                    labware=labware,
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
        labware: Optional[LabwareDefinitionDTO],
    ) -> None:
        """Re-check online, resolve the client, send the command envelope.

        Raises ``DeviceOfflineError`` if the device dropped between
        reservation and send, has no client, or the wire send fails.
        """
        if not await device_connection_tracker.is_device_online(device_id):
            raise DeviceOfflineError(f"Device {device_id} disconnected")

        client_id = await device_connection_tracker.get_client_for_device(device_id)
        if client_id is None:
            raise DeviceOfflineError(f"Device {device_id} has no connected client")

        command_msg = CommandMessage(
            command_id=command_id,
            device_name=device_id,
            command=command,
            params=params,
            effective_mode=effective_mode.value,
            labware=labware,
        )
        envelope = MessageEnvelope.wrap_command(command_msg)
        success = await connection_manager.send_to_client(
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
        command_id). World-sync ops skip the ``_pending`` pop so they don't
        clobber a workflow command's record for the same device.
        """
        async with self._lock:
            self._command_futures.pop(command_id, None)
            if not kind.occupies_the_device:
                return None
            return self._pending.pop(device_id, None)

    async def _command_timeout_watcher(self, pending: _PendingCommand) -> None:
        """Fail an ad-hoc command that overran its timeout + cancel the wire.

        Armed only for ad-hoc (no-execution_id) commands; workflow commands
        are bounded by the engine's recoverable-timeout coordinator. On
        expiry, fail the future and send a ``CancelMessage`` so the device
        bridge drops the in-flight command and a late response can't resolve
        a settled future.

        Cancelled by the disconnect listener while the device is offline;
        re-armed by the reconnect listener with a fresh timer when the
        command is re-sent.
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

    async def _resolve_disconnect_timeout(self, device_id: str) -> float:
        """Resolve the disconnect grace for a device.

        Delegates to :class:`DisconnectGrace`: topology-card override
        first (when registered), then a single conservative fallback.
        """
        return await self._disconnect_grace.grace_for(device_id)

    async def on_device_disconnected(self, device_id: str) -> None:
        """Hold any in-flight command for this device, start the disconnect timer.

        Connection-event listener callable. Idempotent: a duplicate
        disconnect signal for an already-disconnected device is a no-op.
        Wired in main.py via
        ``connection_events.subscribe_disconnected(...)``.
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
        timeout = await self._resolve_disconnect_timeout(device_id)
        async with self._lock:
            # Re-check: a response could have landed while we awaited the
            # resolver; if so, do not arm the disconnect timer.
            pending2 = self._pending.get(device_id)
            if pending2 is None or pending2.future.done():
                return
            pending2.disconnect_timer = asyncio.create_task(
                self._disconnect_timeout_watcher(pending2, timeout),
            )
        logger.info(
            "Device %s disconnected mid-command %s; holding for %ss",
            device_id, pending.command_id, timeout,
        )

    async def on_device_reconnected(self, device_id: str) -> None:
        """Resume an in-flight command after the device reconnected.

        Cancels the disconnect timer, re-sends the original command to
        the new client, and re-arms the command timer. If no command was
        pending (the disconnect happened between commands), this is a
        no-op. Wired via ``connection_events.subscribe_connected(...)``
        in main.py.

        When the pending command opted out of resend (e.g. a
        liquid-handler aspirate / dispense), fail the future with
        ``DeviceOfflineError`` instead of resending. Resending a
        non-idempotent command after a transient drop could double-
        execute on the physical device when the on-prem driver already
        completed the operation before the WebSocket dropped; the
        operator is forced to recover manually instead.
        """
        async with self._lock:
            pending = self._pending.get(device_id)
            if pending is None or pending.future.done():
                return
            if pending.disconnect_timer is None:
                # Reconnect signal without a prior disconnect: nothing to
                # resume. Likely the very first connect for this device.
                return
            disconnect_timer = pending.disconnect_timer
            pending.disconnect_timer = None
        if disconnect_timer is not None and not disconnect_timer.done():
            disconnect_timer.cancel()

        # Opt-out path. Fail the future with a clear LH-no-resend
        # message and drop the pending entry so a follow-up operator
        # command isn't blocked by stale state.
        if not pending.resend_on_reconnect:
            async with self._lock:
                # Re-check under lock: response may have raced in.
                pending2 = self._pending.get(device_id)
                if pending2 is None or pending2.future.done():
                    return
                self._pending.pop(device_id, None)
            logger.warning(
                "Device %s reconnected mid-command %s (%s); refusing resend "
                "because the command is non-idempotent. Manual recovery "
                "required.",
                device_id, pending.command_id, pending.command,
            )
            if not pending.future.done():
                pending.future.set_exception(
                    DeviceOfflineError(
                        f"LH command lost during reconnect; manual recovery "
                        f"required (device={device_id}, "
                        f"command_id={pending.command_id}, "
                        f"command={pending.command})"
                    )
                )
            return

        # Re-send the original command to the new client.
        client_id = await device_connection_tracker.get_client_for_device(
            device_id,
        )
        if client_id is None:
            logger.warning(
                "Reconnect signal for %s but no client registered; "
                "command %s remains pending",
                device_id, pending.command_id,
            )
            return

        command_msg = CommandMessage(
            command_id=pending.command_id,
            device_name=device_id,
            command=pending.command,
            params=pending.params,
            effective_mode=pending.effective_mode.value,
            labware=pending.labware,
        )
        envelope = MessageEnvelope.wrap_command(command_msg)
        success = await connection_manager.send_to_client(
            client_id, envelope.model_dump_json()
        )
        if not success:
            logger.warning(
                "Resend after reconnect failed for command %s on device %s; "
                "treating as still-disconnected",
                pending.command_id, device_id,
            )
            # Trigger the disconnect path again so the next reconnect
            # gets another shot. Don't fail the future here.
            await self.on_device_disconnected(device_id)
            return

        # Re-arm the fail-fast timer only for ad-hoc commands; a workflow
        # command (execution_id set) stays bounded by the engine coordinator,
        # so re-arming a gateway timer here would re-introduce a double bound.
        if pending.execution_id is None:
            async with self._lock:
                pending.command_timer = asyncio.create_task(
                    self._command_timeout_watcher(pending),
                )
        logger.info(
            "Resent command %s on device %s after reconnect",
            pending.command_id, device_id,
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
        client_id = await device_connection_tracker.get_client_for_device(device_id)
        if client_id is None:
            logger.info(
                "No client for device %s; cannot send cancel for command %s",
                device_id, command_id,
            )
            return
        envelope = MessageEnvelope.wrap_cancel(
            CancelMessage(command_id=command_id, reason="engine_cancelled"),
        )
        success = await connection_manager.send_to_client(
            client_id, envelope.model_dump_json(),
        )
        if not success:
            logger.warning(
                "Failed to send cancel for command %s to device %s",
                command_id, device_id,
            )

    async def emergency_unlock(self, device_id: str) -> None:
        """
        Force unlock a device that is stuck in locked state.

        🚨 EMERGENCY USE ONLY 🚨

        This bypasses safety checks and forcibly releases the device lock.
        Only use if you are certain the device is safe to unlock and no
        operation is actually running.

        Use cases:
        - The driver on the device bridge crashed mid-operation
        - Timeout occurred but lock wasn't released
        - Manual recovery after device error

        DO NOT use during normal operations!

        Args:
            device_id: Device to force unlock
        """
        async with self._lock:
            pending = self._pending.pop(device_id, None)

            if pending is None:
                logger.info(f"Device {device_id} is not locked (nothing to unlock)")
                return

            command_id = pending.command_id
            future = self._command_futures.pop(command_id, None)
            if future and not future.done():
                future.cancel()

        # Cancel the record's timers outside the lock (cancel() doesn't
        # await). Leaving them armed would fail / resend a command the
        # operator just force-released.
        self._cancel_timers(pending)
        logger.warning(
            f"EMERGENCY UNLOCK: Forcibly unlocked device {device_id} (was executing {command_id})"
        )


# Global device controller instance
device_controller = DeviceController()
