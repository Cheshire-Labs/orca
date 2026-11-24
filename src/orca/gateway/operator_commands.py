"""The one path an operator's device command takes on every server: `OperatorDeviceCommands`.

It resolves the device, opens an external-control scope on it, holds the device's own lock until the
command's record is saved, and dispatches through the controller. A workflow command does not come through
here: it dispatches through the device facade and never opens an external-control scope.
"""

import copy
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Protocol, TypeVar, runtime_checkable

from cheshire_drivers.command_responses import EmptyCommandResponse
from cheshire_drivers.command_table import DeviceCommand
from pydantic import BaseModel, JsonValue, ValidationError
from typing_extensions import Self

from orca.gateway.controller import IDeviceController
from orca.gateway.controller.exceptions import (
    DeviceLockedError,
    DeviceOfflineError,
    DeviceUnknownError,
    InvalidCommandError,
)
from orca.gateway.device_fault import FAULTED_STATUS, DeviceFaultSummary
from orca.gateway.device_gateway import DeviceGateway
from orca.gateway.controller.command_events import one_command
from orca.gateway.device_command_runner import NamedCommandResult
from orca.gateway.mode_resolution import (
    ResourceLookup,
    mode_of,
    refuse_unbuilt,
    require_system,
)
from orca.gateway.registry import DeviceSnapshot
from orca.operations.error_mapping import error_label
from orca.resource_models.location import Location
from orca.resource_models.reach_in import ARM_REACH_IN_COMMANDS
from orca.resource_models.deck_access import device_behind
from orca.resource_models.resources import IDevice, IResource
from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.transporter_interface import ITransporter
from orca.resource_models.transporter_base import TransporterBase
from orca.resource_models.command_effects import (
    Sender, pick_with_the_arm, place_with_the_arm, reach_params, reach_with_nothing_on_record,
)
from orca.gateway.controller.command_pipeline import send_to_the_device
from orca.gateway.operator_recorder import (
    IOperatorCommandRecorder, RuntimeOperatorCommandRecorder, operator_params_as_sent,
)
from orca.runtime.danger import ActionDescriptor, DangerLevel, record_audit
from orca.modes.run_modes import (
    WorkflowRunMode,
)
from orca.runtime.registry_protocols import IDeviceFaultSource, IDeviceRegistry
from orca.runtime.runtime_interface import ISystemRuntime
from orca.modes.sim_pacing import current_sim_pacer

logger = logging.getLogger(__name__)

_Read = TypeVar("_Read", bound=BaseModel | JsonValue)


@runtime_checkable
class _ExternalControllable(Protocol):
    """Structural shape for an orca-core resource that counts open external-control scopes."""

    @property
    def under_external_control(self) -> bool: ...
    def take_external_control(self) -> None: ...
    def release_external_control(self) -> None: ...


ENGINE_ONLY_WORLD_COMMANDS: dict[str, str] = {
    # Liquid-handler deck occupancy.
    "add_deck_labware": "register-labware",
    "remove_deck_labware": "discharge-labware",
    "reset_deck_labware": "clear-all-labware",
    "reconcile_deck_occupancy": "register-labware / discharge-labware",
    # Transporter world sync.
    "seed_position": "register-labware",
    "ensure_seeded": "register-labware",
    "unseed_position": "discharge-labware",
    "reset_world": "clear-all-labware",
}
"""World-model writes only the engine may send, and the operator verb instead.

Each of these edits a driver's own picture of where labware is. The engine
sends them from the placement chokepoint, which writes the ledger in the same
breath. Sent straight at a device they move only the driver, leaving the ledger
believing the site is empty -- the engine will then route an arm into an
occupied site, and a rebuild will not put the labware back.

The whole set, not the ones a UI happened to send: `reconcile_deck_occupancy`
sets deck occupancy to exactly what the caller names and drops everything else,
which is more destructive than the reset next to it. Every one of these is
unmarked by `@external` in cheshire-drivers, and that only hides them -- hiding
is not refusing, which is the gap this closes.
"""


class EngineOnlyCommandError(InvalidCommandError):
    """An operator aimed a world-model write straight at a device.

    Its own error class rather than a bare ``InvalidCommandError`` so the
    refusal is greppable and a test can name what it protects.
    """


def refuse_engine_only_command(device_id: str, command: str) -> None:
    """Stop a world-model write that would move the driver and not the ledger."""
    instead = ENGINE_ONLY_WORLD_COMMANDS.get(command)
    if instead is None:
        return
    raise EngineOnlyCommandError(
        f"{command!r} is not an operator command on {device_id!r}: it edits the "
        f"driver's model of where labware is and nothing else, so the ledger "
        f"would go on calling the site empty and the labware would not survive "
        f"a rebuild. Use {instead} instead, which writes the record and the "
        f"driver together."
    )


@dataclass(frozen=True)
class DriverAnswer:
    """What the driver returned, and the interface set its typed response is looked up under."""

    raw: JsonValue
    interfaces: frozenset[str]


def _resolve_topology_resource(
    system: ResourceLookup, device_id: str,
) -> IResource | None:
    """The orca-core resource backing ``device_id``, or None.

    Callers pass a system ``require_system`` has already vouched for, so None
    means one thing: the topology does not declare this device. Only the
    external-control flag reads this. The dispatch mode does not, because it
    has to tell that case apart from having no system at all.
    """
    if not system.has_resource(device_id):
        return None
    return system.get_resource(device_id)


def _device_in_topology(
    runtime: ISystemRuntime | None, device_id: str,
) -> bool:
    """Best-effort topology peek: is ``device_id`` declared in the live system?

    Returns ``False`` when the runtime is not yet built so callers fall back
    to the generic ``device_not_found`` path rather than asserting a
    declaration that may exist but is unreadable right now.
    """
    if runtime is None:
        return False
    try:
        for entry in runtime.topology.list_devices():
            if entry.name == device_id:
                return True
    except Exception:
        return False
    return False


def with_fault(faults: IDeviceFaultSource, snapshot: DeviceSnapshot) -> DeviceSnapshot:
    """The snapshot as a reader should see it, with any device fault folded in.

    The device bridge reports its driver as ready because the driver is idle,
    and it has no way to know the control plane will not dispatch to the
    device. A read that passed the device bridge's word through unchanged would
    say "ready" about a machine the workflow cannot touch.

    A copy, so the registry's own record stays the device bridge's report.
    """
    fault = fault_summary(faults, snapshot.name)
    if fault is None:
        return snapshot
    return snapshot.model_copy(update={"status": FAULTED_STATUS, "fault": fault})


def fault_summary(
    faults: IDeviceFaultSource, device_name: str,
) -> DeviceFaultSummary | None:
    """The unresolved fault on this device, shaped for an operator read.

    The one place a surface converts a fault, so no two of them describe the
    same trouble differently.
    """
    fault = faults.fault(device_name)
    return None if fault is None else DeviceFaultSummary.of(fault)


async def resolve_device(
    gateway: DeviceGateway, runtime: ISystemRuntime | None, device_id: str,
) -> DeviceSnapshot:
    """Look up a device in the gateway registry.

    Returns the typed :class:`DeviceSnapshot` when a connection has
    registered it, carrying any fault standing on the device. Raises:
      * :class:`DeviceOfflineError` -- declared in topology but no
        orca-client connection has registered it.
      * :class:`DeviceUnknownError` -- not in topology and not connected.
      * :class:`RuntimeError` -- the registry's auth path (propagated).
    """
    device = await gateway.tracker.get_device(device_id)
    if device is not None:
        return with_fault(gateway.controller, device)
    if _device_in_topology(runtime, device_id):
        raise DeviceOfflineError(
            f"Device {device_id!r} is declared in topology but no "
            "orca-client connection has registered it. Check the "
            "on-prem gateway is running and connected."
        )
    raise DeviceUnknownError(
        f"Device {device_id!r} is not in the running topology and is "
        "not connected via gateway. Verify the device id and that the "
        "deployment_package's topology declares it."
    )


async def resolve_effective_interfaces(registry: IDeviceRegistry, device_id: str) -> frozenset[str] | None:
    """The interfaces a command to this device is checked against, for every sender.

    None when the registry has no such device, so the controller checks against what the connection advertises. A
    registry that fails raises: the command is refused rather than sent against the wider advertised set.
    """
    entry = await registry.get(device_id)
    return None if entry is None else entry.effective_interfaces()


class OperatorControl:
    """Dispatches an operator's commands at one device, and records each one.

    Held open across however many commands the operator sent as one intent, so
    the engine cannot schedule the device between two of them.
    """

    def __init__(
        self,
        runtime: ISystemRuntime,
        device_id: str,
        device: DeviceSnapshot,
        resource: IResource | None,
        effective_mode: WorkflowRunMode,
        recorder: IOperatorCommandRecorder,
        controller: IDeviceController,
    ) -> None:
        self._runtime = runtime
        self._resource = resource
        self._recorder = recorder
        self._device_id = device_id
        self._device = device
        self._effective_mode = effective_mode
        self._controller = controller

    async def run(
        self,
        command: str,
        params: dict[str, JsonValue] | None = None,
        *,
        timeout_seconds: float | None = None,
        confirm: bool = False,
    ) -> DriverAnswer:
        """Send one command through the system's pipeline: checked, sent, then recorded before it counts as done."""
        refuse_engine_only_command(self._device_id, command)
        params = params or {}
        with self._paced():
            if command in ARM_REACH_IN_COMMANDS:
                return await self._reach_in(command, params)
            effective = await resolve_effective_interfaces(self._runtime.device_registry, self._device_id)
            # None leaves the controller to fall back to the connection's advertised set, which the read uses too.
            interfaces = effective if effective is not None else frozenset(self._device.interfaces)
            return await self._runtime.system.command_pipeline.run(
                self._device_id, command,
                lambda: self._dispatch(command, params, timeout_seconds, confirm, effective, interfaces),
                lambda answer: self._save(command, params, answer),
                statement=self._recorder.statement_of(self._device_id, command, params, interfaces),
                checks=(lambda: self._recorder.check_before_sending(command, params, interfaces),),
                params=operator_params_as_sent(command, params, interfaces), execution_id=None, events=None,
                carries_out=None, named=self._recorder.labware_named_by(self._device_id, command, params, interfaces),
                hold=self._holding_the_device(command),
            )

    @contextmanager
    def _paced(self) -> Iterator[None]:
        """A simulated operator command waits at the active run's speed-up, else this server's default, as a run's does."""
        if self._effective_mode is WorkflowRunMode.LIVE or current_sim_pacer.get() is not None:
            yield
            return
        token = current_sim_pacer.set(self._runtime.operator_sim_pacer())
        try:
            yield
        finally:
            current_sim_pacer.reset(token)

    async def _reach_in(self, command: str, params: dict[str, JsonValue]) -> DriverAnswer:
        """An operator's pick or place, made the one way a run's move makes it.

        The transporter brings itself up, resolves its taught position, gateway waypoints and grip, and holds
        the device behind the site across the reach. The pipeline then saves the command, records where the
        plate is and tells the site, as it does for a run.
        """
        mover = self._resource if isinstance(self._resource, TransporterBase) else None
        if mover is None:
            raise NotATransporterError(
                f"{command} reaches into a site, and {self._device_id!r} is not a transporter the topology declares."
            )
        site = _site_of_the_teachpoint(self._runtime, params)
        if site is None:
            raise ReachInAtNoKnownSiteError(
                f"{command} on {mover.name} names no site the topology knows. Orca picks and places only at a "
                f"site, so it can hold the device there and follow the plate. Teach the position at a site, or "
                f"move the arm with move_to_coords."
            )
        await mover.refuse_unless_taught(site.position_id)
        picking = command == "pick_at_coords"
        # None when the record has no plate here: the reach still goes, with the device's default grip.
        labware = site.labware if picking else mover.labware
        device = device_behind(site)
        if device is not None:
            self._refuse_if_busy(device.lock)
        result = DriverAnswer(raw=None, interfaces=frozenset(self._device.interfaces))
        sender = Sender(
            record=lambda _answer: self._save(command, reach_params(site.position_id), result), checks=(),
            execution_id=None, events=None,
        )
        pipeline = self._runtime.system.command_pipeline
        # Checked and taken with nothing awaited between, so the engine cannot take the arm while the site opens.
        self._refuse_if_busy(mover.lock)
        async with mover.lock.held_for(command):
            prepare = site.prepare_for_pick if picking else site.prepare_for_place
            await prepare(labware, mover)
            if labware is None:
                await reach_with_nothing_on_record(pipeline, mover, site, command, sender)
            elif picking:
                await pick_with_the_arm(pipeline, mover, site, labware, sender)
            else:
                await place_with_the_arm(pipeline, mover, site, labware, self._picked_from(mover), sender)
        return result

    def _picked_from(self, mover: TransporterBase) -> Location | None:
        position_id = mover.picked_from_position_id
        if position_id is None:
            return None
        try:
            return self._runtime.system.system_map.get_location(position_id)
        except KeyError:
            return None

    def _refuse_if_busy(self, lock: TrackedLock) -> None:
        """An operator command never queues behind the engine: a move or a shake can take hours."""
        if lock.locked():
            raise DeviceLockedError(
                f"{self._device_id}: {lock.wait_subject}. Send it again once that finishes."
            )

    @asynccontextmanager
    async def _holding_the_device(self, command: str) -> AsyncIterator[None]:
        """The device's own lock, from the checks until the record is saved; refused if anyone holds it.

        Released before the effects: a move's position effect takes this same lock from another task.
        """
        if not isinstance(self._resource, (IDevice, ITransporter)):
            yield
            return
        self._refuse_if_busy(self._resource.lock)
        async with self._resource.lock.held_for(f"operator {command}"):
            yield

    async def _dispatch(
        self, command: str, params: dict[str, JsonValue],
        timeout_seconds: float | None, confirm: bool,
        effective: frozenset[str] | None, interfaces: frozenset[str],
    ) -> DriverAnswer:
        raw = await send_to_the_device(
            self._controller,
            device_id=self._device_id,
            command=command,
            params=params,
            timeout_seconds=timeout_seconds,
            effective_mode=self._effective_mode,
            effective_interfaces=effective,
            confirm=confirm,
        )
        return DriverAnswer(raw=raw, interfaces=interfaces)

    async def _save(
        self, command: str, params: dict[str, JsonValue], result: DriverAnswer,
    ) -> None:
        # The record learns here or the next deck reconcile writes the
        # pre-command state back over the driver. The driver's answer goes too.
        await self._recorder.save_completed(
            self._device_id, command, params, result.raw, result.interfaces,
        )


class ReachInAtNoKnownSiteError(InvalidCommandError):
    """An operator's pick or place at a position no site in the topology names."""


class NotATransporterError(InvalidCommandError):
    """A pick or place sent to a device that is not a transporter the topology declares."""


def _site_of_the_teachpoint(
    runtime: ISystemRuntime, params: Mapping[str, JsonValue],
) -> Location | None:
    """The site an arm teachpoint names, resolved as the ledger resolves it; None for no known site."""
    teachpoint = params.get("teachpoint")
    position_id = teachpoint.get("position_id") if isinstance(teachpoint, dict) else None
    if not isinstance(position_id, str):
        return None
    try:
        return runtime.system.system_map.resolve_placement_location(position_id)
    except KeyError:
        return None


@asynccontextmanager
async def operator_control(
    gateway: DeviceGateway,
    runtime: ISystemRuntime | None,
    device_id: str,
) -> AsyncIterator[OperatorControl]:
    """Resolve the device, open an external-control scope on it, yield a dispatcher.

    One scope per operator intent rather than per command: a batch that opened
    a scope for each of its commands would leave a window between them where the
    engine can take the device, which is the thing sending them together was
    meant to prevent.
    """
    device = await resolve_device(gateway, runtime, device_id)
    # Refused before the scope opens, so a command that never runs leaves none open.
    if runtime is None:
        refuse_unbuilt(device_id)
    system = require_system(runtime.system, device_id)
    resource = _resolve_topology_resource(system, device_id)
    effective_mode = await mode_of(resource, when_unseeded=runtime.operator_base_mode())
    external_resource = (
        resource if isinstance(resource, _ExternalControllable) else None
    )
    if external_resource is not None:
        external_resource.take_external_control()
    try:
        yield OperatorControl(
            runtime, device_id, device, resource, effective_mode,
            RuntimeOperatorCommandRecorder(runtime), gateway.controller,
        )
    finally:
        if external_resource is not None:
            external_resource.release_external_control()


class UnreadableDriverAnswerError(Exception):
    """The driver's answer does not fit the response model its command declares."""


def _typed(command: DeviceCommand) -> Callable[[DriverAnswer], BaseModel]:
    def read(answer: DriverAnswer) -> BaseModel:
        return typed_response(answer.raw, command.response_model, command.name)
    return read


def _untyped(answer: DriverAnswer) -> JsonValue:
    return answer.raw


def typed_response(raw: JsonValue, response: type[BaseModel], command: str) -> BaseModel:
    """The driver's answer as the command's response model: nothing, a model's fields, or one bare value."""
    if raw is None:
        return EmptyCommandResponse()
    try:
        if isinstance(raw, dict):
            return response.model_validate(raw)
        fields = list(response.model_fields)
        if isinstance(raw, (bool, float, int, str)) and len(fields) == 1:
            return response.model_validate({fields[0]: raw})
    except ValidationError as exc:
        raise UnreadableDriverAnswerError(
            f"the driver's answer to {command!r} does not fit {response.__name__}: {exc}"
        ) from exc
    raise UnreadableDriverAnswerError(
        f"the driver's answer to {command!r} is a {type(raw).__name__}, which {response.__name__} cannot hold"
    )


OPERATOR_COMMAND = ActionDescriptor(
    name="device.execute", danger_level=DangerLevel.PHYSICAL, requires_reason=False,
    message="Send command '{command}' to device '{device_name}'. Bypasses workflow coordination; "
            "concurrent actions may contend on the device lock and leave hardware in an unexpected state.",
)

LIFECYCLE_COMMANDS: dict[str, ActionDescriptor] = {
    "initialize": ActionDescriptor(
        name="device.initialize", danger_level=DangerLevel.PHYSICAL, requires_reason=False,
        message="Initialize device '{device_name}'. Brings it up and resets driver state -- can drop the labware "
                "it was tracking, reset calibration, and interrupt an action currently holding the device lock. "
                "It asks for no motion: ask for `home` when you want the device moved. Check "
                "`orca device info {device_name}` first.",
    ),
    "connect": ActionDescriptor(
        name="device.connect", danger_level=DangerLevel.OPERATOR, requires_reason=False,
        message="Connect device '{device_name}': open its link and take its session. Moves nothing.",
    ),
    "disconnect": ActionDescriptor(
        name="device.disconnect", danger_level=DangerLevel.OPERATOR, requires_reason=False,
        message="Disconnect device '{device_name}'. Ends the session and drops motor power on hardware that "
                "holds it, so anything mid-action on this device fails.",
    ),
}


def _audit(
    device_id: str, command: str, params: Mapping[str, JsonValue], *, confirm: bool, reason: str | None,
) -> None:
    """One audit row per operator command, whichever route sent it; a lifecycle command under its own verb.

    A read moves nothing, so it writes none.
    """
    if command.startswith("get_"):
        return
    lifecycle = LIFECYCLE_COMMANDS.get(command)
    if lifecycle is not None:
        record_audit(lifecycle, reason, {"device_name": device_id})
        return
    record_audit(OPERATOR_COMMAND, reason, {
        "device_name": device_id, "command": command, "options": dict(params), "vendor_confirm": confirm,
    })


class OperatorDeviceCommands:
    """The operator path to every device on one server: one audit row, then the command, for every route."""

    def __init__(
        self,
        gateway: DeviceGateway,
        runtime: Callable[[], ISystemRuntime | None],
    ) -> None:
        self._gateway = gateway
        self._runtime = runtime
        self._held: Mapping[str, OperatorControl] = {}

    async def device(self, device_id: str) -> DeviceSnapshot:
        return await resolve_device(self._gateway, self._runtime(), device_id)

    async def run(
        self, device_id: str, command: DeviceCommand, params: BaseModel, *, reason: str | None = None,
    ) -> BaseModel:
        sent = params.model_dump(mode="json", exclude_unset=True)
        _audit(device_id, command.name, sent, confirm=False, reason=reason)
        response, _ = await self._send(
            device_id, command.name, sent, timeout_seconds=None, confirm=False, read=_typed(command),
        )
        return response

    async def run_named(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None = None, confirm: bool = False, reason: str | None = None,
    ) -> NamedCommandResult:
        _audit(device_id, command, params, confirm=confirm, reason=reason)
        value, duration_ms = await self._send(
            device_id, command, params, timeout_seconds=timeout_seconds, confirm=confirm, read=_untyped,
        )
        return NamedCommandResult(value=value, duration_ms=duration_ms)

    @asynccontextmanager
    async def hold(self, device_id: str) -> AsyncIterator[Self]:
        if device_id in self._held:
            yield self
            return
        async with operator_control(self._gateway, self._runtime(), device_id) as control:
            holding = copy.copy(self)
            holding._held = {**self._held, device_id: control}
            yield holding

    def _control(self, device_id: str) -> AbstractAsyncContextManager[OperatorControl]:
        held = self._held.get(device_id)
        if held is not None:
            return nullcontext(held)
        return operator_control(self._gateway, self._runtime(), device_id)

    async def _send(
        self, device_id: str, command: str, params: Mapping[str, JsonValue],
        *, timeout_seconds: float | None, confirm: bool, read: Callable[[DriverAnswer], _Read],
    ) -> tuple[_Read, float]:
        started = time.monotonic()
        async with one_command(
            device_id, command, params, error_text=error_label, events=self._gateway.controller.command_events,
        ) as block:
            # The arm builds a pick or place request itself, so its events carry that request, as a run's do.
            block.params_named = command not in ARM_REACH_IN_COMMANDS
            async with self._control(device_id) as control:
                answer = await control.run(command, dict(params), timeout_seconds=timeout_seconds, confirm=confirm)
            value = read(answer)
        return value, (time.monotonic() - started) * 1000
