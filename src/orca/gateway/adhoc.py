"""Generic ad-hoc device-command execution: the engine-bypassing direct path.

Resolves a device in the gateway registry, takes the external-control flag,
applies the capability-drift intersection, and dispatches through
``device_controller``. Returns the RAW driver result plus the interface set
used; typed response coercion and error-envelope shaping belong to the
caller's surface layer. The workflow engine path does NOT go through here --
it dispatches via the device facade and never takes the external-control flag,
so workflow commands and ad-hoc commands cannot fight over the same device.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable

from pydantic import JsonValue

from orca.gateway.controller import device_controller
from orca.gateway.device_fault import FAULTED_STATUS, DeviceFaultSummary
from orca.gateway.mode_resolution import ResourceLookup, mode_of, require_system
from orca.runtime.run_modes import (
    OPERATOR_DEVICE_WRITE_BASE,
    WorkflowRunMode,
    mode_scope,
)
from orca.resource_models.resources import IResource
from orca.gateway.controller.exceptions import (
    DeviceOfflineError,
    DeviceUnknownError,
    InvalidCommandError,
)
from orca.gateway.registry import DeviceSnapshot, device_connection_tracker
from orca.runtime.adhoc_ledger import record_operator_command
from orca.runtime.runtime_interface import ISystemRuntime

logger = logging.getLogger(__name__)


@runtime_checkable
class DeviceDispatch(Protocol):
    """The one call a control scope makes at a device.

    Named as a shape so a caller that already holds a controller passes its own
    rather than the scope reaching for the module-level one behind its back.
    """

    async def execute_command(
        self, *, device_id: str, command: str, params: dict[str, JsonValue],
        timeout_seconds: float | None, effective_mode: WorkflowRunMode,
        effective_interfaces: frozenset[str] | None = None,
        confirm: bool = False,
    ) -> JsonValue: ...


@runtime_checkable
class _ExternalControllable(Protocol):
    """Structural shape for an orca-core resource with the external-control flag."""

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
class AdhocResult:
    """Raw driver return plus the interface set to use for response lookup.

    The generic mechanism returns the untyped wire result; the caller's
    surface layer projects ``raw`` onto a typed response using ``interfaces``.
    """

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
    to the generic ``device_unknown`` path rather than asserting a
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


def with_fault(snapshot: DeviceSnapshot) -> DeviceSnapshot:
    """The snapshot as a reader should see it, with any device fault folded in.

    The device bridge reports its driver as ready because the driver is idle,
    and it has no way to know the control plane will not dispatch to the
    device. A read that passed the device bridge's word through unchanged would
    say "ready" about a machine the workflow cannot touch.

    A copy, so the registry's own record stays the device bridge's report.
    """
    fault = fault_summary(snapshot.name)
    if fault is None:
        return snapshot
    return snapshot.model_copy(update={"status": FAULTED_STATUS, "fault": fault})


def fault_summary(device_name: str) -> DeviceFaultSummary | None:
    """The unresolved fault on this device, shaped for an operator read.

    The one place a surface converts a fault, so no two of them describe the
    same trouble differently.
    """
    fault = device_controller.fault(device_name)
    return None if fault is None else DeviceFaultSummary.of(fault)


async def list_devices(
    device_type: str | None = None,
    site: str | None = None,
    lab: str | None = None,
    status: str | None = None,
) -> list[DeviceSnapshot]:
    """Every connected device, each carrying its own fault.

    The one device list an operator surface should call. ``status`` filters on
    the folded value, so asking for faulted devices works and asking for ready
    ones does not quietly include them.
    """
    devices = [
        with_fault(device)
        for device in await device_connection_tracker.list_devices(
            device_type=device_type, site=site, lab=lab,
        )
    ]
    if status:
        devices = [device for device in devices if device.status == status]
    return devices


async def resolve_device(
    runtime: ISystemRuntime | None, device_id: str,
) -> DeviceSnapshot:
    """Look up a device in the gateway registry.

    Returns the typed :class:`DeviceSnapshot` when a connection has
    registered it, carrying any fault standing on the device. Raises:
      * :class:`DeviceOfflineError` -- declared in topology but no
        orca-client connection has registered it.
      * :class:`DeviceUnknownError` -- not in topology and not connected.
      * :class:`RuntimeError` -- the registry's auth path (propagated).
    """
    device = await device_connection_tracker.get_device(device_id)
    if device is not None:
        return with_fault(device)
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


async def resolve_effective_interfaces(
    runtime: ISystemRuntime | None,
    device_id: str,
    advertised_fallback: frozenset[str],
) -> frozenset[str] | None:
    """Compute the dispatch interface set from the unified registry.

    Both cards present -> declared INTERSECT advertised (the capability-drift
    block: a driver on the device bridge advertising a wider set than topology
    declared cannot dispatch undeclared interface methods), unless topology
    never got a declaration and is holding the driver class's defaults, in
    which case advertised wins. Connection-only -> advertised (quarantine;
    vendor extras still count). Topology-only -> declared. Runtime not ready /
    no entry / lookup error -> ``None`` so the controller falls back to the
    connection tracker's advertised set.
    """
    if runtime is None:
        return None
    try:
        entry = await runtime.device_registry.get(device_id)
    except Exception:
        return None
    if entry is None:
        return None
    if entry.topology_card is not None and entry.connection_card is not None:
        if entry.topology_card.declared_interfaces_are_class_defaults:
            # Nothing was declared: the runtime was built before this device's
            # client connected, so topology holds the driver class's defaults.
            # Intersecting with those refuses every command past the base kind
            # while the catalog still lists them -- a Flex's `home` among them.
            return entry.connection_card.advertised_interfaces
        return (
            entry.topology_card.declared_interfaces
            & entry.connection_card.advertised_interfaces
        )
    if entry.connection_card is not None:
        return entry.connection_card.advertised_interfaces
    if entry.topology_card is not None:
        return entry.topology_card.declared_interfaces
    return advertised_fallback


async def execute_adhoc_command(
    runtime: ISystemRuntime | None,
    device_id: str,
    command: str,
    params: dict[str, JsonValue] | None = None,
    *,
    timeout_seconds: float | None = None,
    confirm: bool = False,
) -> AdhocResult:
    """Resolve the device, take external control, dispatch via the controller.

    Returns the raw driver result plus the interface set used (so the caller
    can look up the typed response). Raises the controller exceptions
    (:class:`DeviceOfflineError` / :class:`DeviceUnknownError` /
    :class:`DeviceLockedError` / :class:`InvalidCommandError` /
    :class:`CommandTimeoutError` / :class:`CommandExecutionError` /
    :class:`ModeUnresolvableError`) and ``RuntimeError`` straight through --
    response coercion and error-envelope shaping are the caller's job. The
    external-control flag is taken for the duration of the dispatch and
    released on every exit path.

    ``ModeUnresolvableError`` is the unbuilt-runtime case: the device may be
    connected and answerable, but its topology declaration is not readable, and
    that declaration is what would hold the command back from real hardware.

    The workflow engine bypasses this function (it dispatches via the device
    facade), so workflow commands never take the external-control flag.
    """
    async with operator_control(runtime, device_id) as control:
        return await control.run(
            command, params, timeout_seconds=timeout_seconds, confirm=confirm,
        )


class OperatorControl:
    """Dispatches an operator's commands at one device, and records each one.

    Held open across however many commands the operator sent as one intent, so
    the engine cannot schedule the device between two of them.
    """

    def __init__(
        self,
        runtime: ISystemRuntime | None,
        device_id: str,
        device: DeviceSnapshot,
        effective_mode: WorkflowRunMode,
        controller: DeviceDispatch | None = None,
    ) -> None:
        self._runtime = runtime
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
    ) -> AdhocResult:
        refuse_engine_only_command(self._device_id, command)
        effective_interfaces = await resolve_effective_interfaces(
            self._runtime, self._device_id, frozenset(self._device.interfaces),
        )
        # Resolved per call, never bound as a default: the module attribute is
        # what tests patch, and a default would have captured the original.
        controller = self._controller or device_controller
        raw = await controller.execute_command(
            device_id=self._device_id,
            command=command,
            params=params or {},
            timeout_seconds=timeout_seconds,
            effective_mode=self._effective_mode,
            effective_interfaces=effective_interfaces,
            confirm=confirm,
        )
        # The ledger learns here or the next deck reconcile writes the
        # pre-command state back over the driver.
        if self._runtime is not None:
            # Under the same mode the command itself ran: the recording projects
            # onto a driver, and an unseeded read resolves the sim one.
            with mode_scope(self._effective_mode):
                await record_operator_command(
                    self._runtime.system, self._device_id, command, params or {},
                )
        interfaces = (
            effective_interfaces
            if effective_interfaces is not None
            else frozenset(self._device.interfaces)
        )
        return AdhocResult(raw=raw, interfaces=interfaces)


@asynccontextmanager
async def operator_control(
    runtime: ISystemRuntime | None,
    device_id: str,
    controller: DeviceDispatch | None = None,
) -> AsyncIterator[OperatorControl]:
    """Resolve the device, hold the external-control flag, yield a dispatcher.

    One scope per operator intent rather than per command: a batch that took
    the flag for each of its commands leaves a window between them where the
    engine can take the device, which is the thing sending them together was
    meant to prevent.
    """
    device = await resolve_device(runtime, device_id)
    # Refused before external control is taken, so a command that never runs
    # does not leave the flag set.
    system = require_system(
        runtime.system if runtime is not None else None, device_id,
    )
    resource = _resolve_topology_resource(system, device_id)
    effective_mode = mode_of(resource, when_unseeded=OPERATOR_DEVICE_WRITE_BASE)
    external_resource = (
        resource if isinstance(resource, _ExternalControllable) else None
    )
    if external_resource is not None:
        external_resource.take_external_control()
    try:
        yield OperatorControl(
            runtime, device_id, device, effective_mode, controller,
        )
    finally:
        if external_resource is not None:
            external_resource.release_external_control()
