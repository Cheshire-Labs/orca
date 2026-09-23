"""Frozen snapshot dataclasses for querying runtime state.

These are point-in-time, read-only views of the live execution object graph.
Consumers (CLI, future REST/MCP) import ONLY these types; they must not
import live internal objects like `ExecutingLabwareThread`, `LabwareInstance`,
`Device`, etc. That one-way coupling shields UIs from internal refactors.

Snapshots are built on demand inside facade methods and not cached across
calls. A snapshot returned from one call does NOT auto-update; callers
re-query to see current state.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from cheshire_drivers.gateway_protocol import EffectiveMode
from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.gateway.device_fault import DeviceFaultSummary
from orca.state.provenance import Provenance
from pydantic import BaseModel, ConfigDict, JsonValue

from orca.state.placement import PlacementState
from orca.runtime.danger import ActionDescriptor, DangerLevel, ParamSpec
from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.run_modes import (
    WorkflowRunMode,
    resolve_effective_mode_for_device,
)
from orca.runtime.submission_modes import BatchMode, SubmissionStatus
from orca.workflow_models.status_enums import FailurePolicy


@dataclass(frozen=True)
class ActionSnapshot:
    id: str
    command: str
    status: str
    position_id: str
    resource_name: str
    description: str | None = None


@dataclass(frozen=True)
class MethodSnapshot:
    id: str
    name: str
    status: str
    current_action: ActionSnapshot | None
    completed_action_count: int


@dataclass(frozen=True)
class ThreadSnapshot:
    id: str
    name: str
    status: str
    current_location: str
    current_method: MethodSnapshot | None
    completed_method_count: int
    last_error: str | None                    # set when status is PAUSED due to action error
    pause_reason: str | None                  # "manual" | "system" | "error" | None (not paused)
    completed_methods: tuple[str, ...]        # template names of finished methods, in execution order
    labware_template_name: str | None = None  # the labware this thread carries; None for threads without a labware (e.g. join-only/synthetic threads). Operator surfaces use this to pre-validate insert-method against the method's action inputs.
    labware_id: str | None = None             # identity of the carried instance, so an operator surface can act on it (discharge a manual-remove park) without knowing that a thread's id IS its labware's id. Equal to `id` by construction; named separately so a client never has to rely on that.
    labware_name: str | None = None           # display name of the carried instance; equal to `name` for the same reason
    paused_device_command: str | None = None  # the device call the thread is suspended inside, from the op-level recovery seam (the group's when it is a contributor to a paused shared action). None at every other pause, which is exactly when the runtime refuses RETRY_OP.
    pause_message: str | None = None          # what the thread was doing when it stopped, in words: which seam raised and, for a device call, which call, on an error pause; the system's own cause (stall, deadlock, timeout, quarantine) on a system pause. `last_error` is the exception alone and cannot say.
    pause_site: str | None = None             # WHERE this thread stopped, or None when it is not error-paused. See `PauseSite`.
    honoured_decisions: tuple[str, ...] = ()  # the recovery decisions this thread will accept right now, and the only ones to offer. Empty when it is not error-paused. Anything else is refused on the call and the thread stays paused. Derived from `pause_site` via `HONOURED_DECISIONS`, so a surface never has to know the rule.
    waiting_for: str | None = None            # specific subject of this thread's wait: the device or transporter lock it is queued for (any status), else target location name or the comma-joined candidate list for an end-of-thread move (move waits), comma-joined missing labware names (co-thread waits), method name (action resolution). None when the thread is not waiting or the subject is not reachable from the snapshot context.


@dataclass(frozen=True)
class ReservationSnapshot:
    position_id: str
    reservation_id: str
    # ``thread_id`` is None for reservations not owned by any thread
    # (system-held / manual holds). Wire-shape change from prior
    # ``str`` typing that masked None as the literal ``"unknown"``.
    thread_id: str | None
    # Populated by the cross-execution daemon route. Per-execution routes
    # leave it None: the URL already pins the scope.
    execution_id: str | None = None
    # What the hold is FOR. A surface that only knows a position is held can
    # invite a placement the engine is about to refuse.
    labware_name: str | None = None
    # What the hold IS. Both come off the one reservation tier, so they cannot
    # disagree: awaiting_operator is a person being asked to place the labware
    # named above, arriving is that labware on its way here. A hold that is
    # neither (a source held while its plate is carried, a device an action
    # needs, a corridor seat) reports False twice, and a surface must not read
    # it as somewhere labware is about to land.
    awaiting_operator: bool = False
    arriving: bool = False


@dataclass(frozen=True)
class PendingManualStepRecord:
    """One emitted-but-unconfirmed operator manual step, tagged with its execution."""

    execution_id: str
    step_id: str
    instruction: str
    emitted_at: datetime


@dataclass(frozen=True)
class ExecutionDetail:
    id: str
    workflow_name: str
    status: str
    error: str | None
    threads: list[ThreadSnapshot]
    total_thread_count: int
    completed_thread_count: int
    active_thread_count: int
    paused: bool = False
    """Whether the execution-level pause latch is set.

    Orthogonal to ``status``: a paused execution stays ACCEPTING or DRAINING,
    so a surface reading only the phase shows a stopped run as still running.
    """
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself (a stall, an unresolvable deadlock). A recoverable
    timeout pauses the threads without the latch, so it reads not-paused
    here and shows on the thread statuses instead. None when not paused."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort. Set by the first stop
    call, cleared by resume."""


@dataclass
class ExecutionStatus:
    """Immutable snapshot of an execution's state for external consumers.

    ``status`` is an ``ExecutionPhase`` (str-backed enum); equality against
    legacy lowercase strings like ``"completed"`` still works. Used by
    surfaces that want the rich phase without paying for thread snapshots
    (``ExecutionDetail`` builds those eagerly).
    """
    id: str
    workflow_name: str
    status: ExecutionPhase
    error: str | None = None
    paused: bool = False
    """Whether the execution-level pause latch is set. Orthogonal to
    ``status``: a paused execution stays ACCEPTING or DRAINING."""
    pause_reason: str | None = None
    """Who set the latch: `manual` for an operator's stop, `system` when the
    runtime paused itself. None when not paused."""
    abort_armed: bool = False
    """Whether a second confirmed stop would abort."""


# -- Labware + location --------------------------------------------------


@dataclass(frozen=True)
class LabwareSnapshot:
    id: str
    name: str
    template_name: str
    barcode: str | None
    current_location: str | None
    placement: PlacementState | None = None
    """Whether ``current_location`` is where it IS or where it is EXPECTED.

    A thread knows its labware and where that labware belongs before an
    operator has put anything down, so a location alone does not mean the
    labware is there. None when nothing has ever placed it anywhere.
    """
    carry_override: MoveParameterPatch = field(default_factory=MoveParameterPatch)
    """How this one piece of labware is being carried; empty for almost all."""
    contents_provenance: Provenance = Provenance.UNKNOWN
    """How well the record knows what this labware holds.

    STALE is the one worth acting on: a stretch went unobserved (a
    restart, a reconnect, an error pause) and nobody has looked since. UNKNOWN
    means nothing has ever said, which is not the same as an empty labware.
    """


@dataclass(frozen=True)
class TipStateSnapshot:
    """A rack's tip layout as the ledger folds it, and how well it knows it."""
    labware_id: str
    positions_present: list[str]
    provenance: Provenance


@dataclass(frozen=True)
class WellVolumesSnapshot:
    """A labware's per-well volumes, and how well the record knows them.

    The volumes used to travel on their own. A bare number looks equally sure
    whether an operator stated it a minute ago or a restart left it describing
    a plate somebody has since emptied, which is the whole reason provenance
    rides every other contents read.
    """
    labware_id: str
    volumes: dict[str, float]
    provenance: Provenance


@dataclass(frozen=True)
class LocationEvent:
    """One entry in a labware's location history.

    Every record self-describes when it happened. ``timestamp`` is
    seconds-since-epoch stamped by the labware store on every
    ``update_location`` call; required because every Record-shaped
    runtime entity needs a time axis (cross-merging with ops_history,
    chronological journey rendering, audit ordering).
    """
    sequence: int                              # 0-based position in the history
    position_id: str
    timestamp: float


@dataclass(frozen=True)
class LocationSnapshot:
    name: str
    resource_name: str | None                 # device/transporter mounted at this location (if any)
    loaded_labware_ids: tuple[str, ...]       # ids of labware currently tracked as at this location
    # Deck sites are not routing nodes, so the listing is the only place they
    # surface. Empty for everything except a device with a deck layout.
    deck_sites: tuple[str, ...] = ()          # addressable child sites, e.g. "mlstar_1/carrier-9-0"


# -- Resources -----------------------------------------------------------


@dataclass(frozen=True)
class DeviceSnapshot:
    """Per-device snapshot for operator reads.

    Every field describes ONE world, the one an operator's own verbs act in.
    `effective_mode` resolves against `OPERATOR_DEVICE_WRITE_BASE` whatever run
    may be in force, because `is_initialized` beside it comes from
    `DeviceLinkReader`, which resolves that same base so a `connect` or
    `initialize` moves flags the reader can see. Resolving the mode against
    anything else puts a sim-world mode next to a live-world flag in one row,
    and a reader believes whichever half they looked at.

    A topology `sim_override` ratchets the answer toward sim on top of that
    base, so a device declared sim reads sim. Toward-live overrides are inert
    per the v3.4 resolver.

    What this does NOT answer is what a WORKFLOW will do. A submission carries
    its own `run_mode`, so a PURE_SIM submission dispatches PURE_SIM to a
    device this row reports LIVE for. Read `sim_override` to learn how a device
    is configured, and the submission's `run_mode` for what a run will do.
    """
    name: str
    type_name: str                            # class name, e.g. "Shaker", "Sealer"
    is_initialized: bool
    is_busy: bool                             # device lock currently held
    effective_mode: WorkflowRunMode           # resolved against a base mode; see class doc
    position_ids: tuple[str, ...]           # where this device is mounted on the system map
    loaded_labware_ids: tuple[str, ...]
    # True when a hosted device-integration gateway holds the device for
    # ad-hoc troubleshooting; Orca dispatch refuses while this is set.
    # See ``Device.under_external_control``. Defaulted so existing
    # in-repo construct-sites that pre-date this field don't break --
    # the gateway is the only writer and it's wired up post-merge by
    # a hosted deployment. Reason / audit trail lives in operations history + the
    # pause UI; the wire field is just the flag.
    under_external_control: bool = False
    external_control_hold: str | None = None
    """Why an operator is holding this, or None if nobody is.

    ``under_external_control`` is also true for the instant the gateway holds
    it around one ad-hoc command. This field is set only by the standing
    operator claim, which lasts until it is released; empty string means held
    with no reason given."""
    fault: DeviceFaultSummary | None = None
    """The command that left this device part-way through something, or None.

    Set when a command failed, timed out, or was cancelled after it went out on
    the wire, and it stands until an operator clears it. While it is set the
    workflow cannot drive the device."""


@dataclass(frozen=True)
class DeviceIntrospection:
    """Static driver introspection for a registered device.

    Sources `interfaces`, `capabilities` (auto-derived vendor extras), and the
    per-method `methods` dict from cheshire-drivers' `driver_introspection`
    helpers, plus `provides_state` from the driver class. Drives the
    `orca device capabilities <id>` CLI subcommand and the daemon's NEW
    `GET /devices/{name}/introspection` route -- the daemon-side mirror of
    a hosted deployment's existing `GET /api/devices/{id}/capabilities`.

    Field shapes match a hosted deployment's payload byte-for-byte so the same CLI rendering
    works against either backend. `name` is the device identity; there is no
    separate `device_id` (a hosted deployment's capabilities response carries none).
    """
    type: str
    name: str
    interfaces: tuple[str, ...]
    capabilities: tuple[str, ...]
    provides_state: bool
    methods: dict[str, dict[str, JsonValue]]


@dataclass(frozen=True)
class TopologyDeviceEntry:
    """What `topology.py` declared for a device.

    Built by `SystemTopologyRegistry` from `system.resource_registry`. Carries
    the operator-visible identity (name) plus the contract the deployment
    expects: the driver class that should serve this device, and the
    capability interfaces declared on the driver. Used by the topology x
    gateway collision validator to refuse mismatched gateway connections.

    `sim_override` carries the per-device run-mode override declared at
    construction time (e.g. `Shaker(name=..., sim_override=PURE_SIM)`). Used
    by the unified DeviceRegistry to populate `TopologyCard.topology_sim_override`
    so the mode resolver can pick it up in the override hierarchy.
    """
    name: str
    kind: str
    interfaces: tuple[str, ...]
    position_ids: tuple[str, ...]
    sim_override: WorkflowRunMode | None = None
    # True when `interfaces` is the driver class's default set rather than what
    # this device advertised. Required, not defaulted: a construction path that
    # forgot it would read as a real declaration and narrow a dispatch gate.
    interfaces_are_class_defaults: bool = field(kw_only=True)


@dataclass(frozen=True)
class GatewayDeviceEntry:
    """What the gateway sees for a connected device.

    Built by `IGatewayRegistry` implementations from on-prem orca-client
    connection state. `last_heartbeat` is None when the gateway tracks the
    device by metadata only (e.g. cold-start from the persisted
    `RegisteredDevice` row before a reconnect). `driver_class_observed`
    reflects the class orca-client advertised at handshake; collisions
    against the topology declaration are rejected loudly.
    """
    name: str
    driver_class_observed: str
    interfaces: tuple[str, ...]
    last_heartbeat: datetime | None
    connection_id: str | None
    status: str



class TopologyCard(BaseModel):
    """What topology declared for a device.

    Built from the System resource graph at registry-query time. Carries the
    operator-visible identity (name) plus the contract the deployment expects
    (declared kind, declared interface set, mounting locations) plus the
    optional per-device sim override (`topology_sim_override`) that lets a
    deployment force a specific device into sim under a non-PURE_SIM base
    mode.

    `disconnect_timeout_seconds` configures the per-device pause-and-wait
    timeout for the disconnect policy; carried here so topology authors
    can tune it per device. None means "use the runtime default for
    the device kind."
    """

    model_config = ConfigDict(frozen=True)

    name: str
    declared_kind: str
    declared_interfaces: frozenset[str]
    mounting_locations: tuple[str, ...]
    topology_sim_override: WorkflowRunMode | None = None
    disconnect_timeout_seconds: float | None = None
    # See `TopologyDeviceEntry.interfaces_are_class_defaults`. Dispatch reads
    # this to tell "declared narrower on purpose" from "nothing declared yet".
    declared_interfaces_are_class_defaults: bool


class ReportedDeviceLink(BaseModel):
    """One device's link state, as answered by whoever is driving it.

    `mode` names which of the device's drivers answered, and reading it is not
    optional. An open DEVICE_SIM link reported as plain "connected" is a
    simulator read as the instrument; an open LIVE link reported that way on a
    bench dispatching DEVICE_SIM is the instrument's answer shown for commands
    that will never reach it. PURE_SIM says the answer came from orca's own
    in-process simulator, which is where a device the topology declares sim
    lands however the caller asks.

    `mode` is None only when nobody can answer: a device bridge has advertised
    the device but not reported yet, or it advertised it and went away, leaving
    an in-process stand-in whose flags are a stale cache. Both flags read False
    there, because a boolean has nowhere to put "unknown".
    """

    model_config = ConfigDict(frozen=True)

    mode: EffectiveMode | None
    is_connected: bool
    is_initialized: bool


class ConnectionCard(BaseModel):
    """What the gateway sees for a connected device.

    Built from on-prem orca-client WebSocket connection state. `last_heartbeat`
    is None when the gateway tracks the device by metadata only (e.g., a
    transient cold-start state where the connection was registered before any
    heartbeat lands).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    client_id: str
    connection_id: str
    last_heartbeat: datetime | None
    advertised_kind: str
    advertised_interfaces: frozenset[str]
    # The device bridge holds the driver, so these are the only truthful
    # answers about the device's own link. Read `device_link_mode` with them:
    # see `ReportedDeviceLink`. False with no mode until the first status
    # report lands, which follows the handshake on the same socket.
    device_is_connected: bool = False
    device_is_initialized: bool = False
    device_link_mode: EffectiveMode | None = None


class _DeviceLiveQueryProvider(Protocol):
    """Narrow surface DeviceRegistryEntry uses to answer live-state queries.

    Lives here (instead of runtime_interface) to avoid an import cycle: this
    module is imported by runtime_interface, so the cycle would loop back.
    `IDeviceRegistry` in runtime_interface.py satisfies this Protocol
    structurally; tests can pass a fake.
    """

    async def _is_client_connected(self, name: str) -> bool: ...


class DeviceRegistryEntry:
    """Two-card view of a device: topology + connection, aligned by name.

    Either card may be present alone. Topology-only entries support
    PURE_SIM only (per R1 in the device-registry-reconciliation plan).
    Connection-only entries are accepted for direct REST/MCP dispatch but
    excluded from workflow scheduling because workflows reference devices
    declared in topology.

    `is_client_connected` is async because it delegates live to the owning
    IDeviceRegistry. The device's own link is read once when the entry is built
    and carried whole, so its two flags and the mode they came from cannot
    describe different observations of a roster a disconnect can change between
    reads. Nothing auto-updates: re-query for a newer answer.
    """

    def __init__(
        self,
        name: str,
        topology_card: TopologyCard | None,
        connection_card: ConnectionCard | None,
        registry: _DeviceLiveQueryProvider,
        device_link: ReportedDeviceLink,
    ) -> None:
        self.name = name
        self.topology_card = topology_card
        self.connection_card = connection_card
        self._registry = registry
        self._device_link = device_link

    async def is_client_connected(self) -> bool:
        """Whether the on-prem client that owns this device is reachable.

        A per-CLIENT heartbeat, reported against every device that client owns,
        so it says nothing about this device in particular. Named for what it
        measures: read as "is the device connected" it will claim a released
        device is available.
        """
        return await self._registry._is_client_connected(self.name)

    async def is_device_connected(self) -> bool:
        """Whether THIS device's own link is open, on the driver being driven.

        The second of the two connections. Answered by the on-prem device
        bridge when one holds the device, and otherwise by the driver orca's
        own `connect` / `disconnect` dispatch through. So it is false for a
        released device even while its client keeps heartbeating. Read
        `device_link_mode` with it: without the mode a simulator's open link
        reads as the instrument.
        """
        return self._device_link.is_connected

    async def is_initialized(self) -> bool:
        """Whether THIS device has been brought up. Same source as the link."""
        return self._device_link.is_initialized

    def device_link_mode(self) -> EffectiveMode | None:
        """Which driver answered the two flags above, or None if nobody could.

        Nobody could covers three cases and they read the same on the wire: a
        device bridge holds this device and has not reported yet, a device
        bridge held it and went away, or the system holds no resource by this
        name. Both flags are False in all three.
        """
        return self._device_link.mode

    def declared_kind(self) -> str | None:
        if self.topology_card is None:
            return None
        return self.topology_card.declared_kind

    def advertised_kind(self) -> str | None:
        if self.connection_card is None:
            return None
        return self.connection_card.advertised_kind

    def effective_interfaces(self) -> frozenset[str]:
        """Operative interface set for capability dispatch.

        When both cards are present, returns the connection's advertised set
        (verified to be a superset of the topology's declared set per C2 at
        composition time). When only one card is present, returns that card's
        interfaces.
        """
        if self.connection_card is not None:
            return self.connection_card.advertised_interfaces
        if self.topology_card is not None:
            return self.topology_card.declared_interfaces
        return frozenset()

    def topology_sim_override(self) -> WorkflowRunMode | None:
        if self.topology_card is None:
            return None
        return self.topology_card.topology_sim_override

    async def supports_mode(self, mode: WorkflowRunMode) -> bool:
        """True iff this entry can run a workflow submitted under `mode`.

        Resolves the per-device effective mode first: a topology `sim_override`
        can ratchet a device to PURE_SIM under a LIVE / DEVICE_SIM submission,
        in which case it dispatches to its sim driver and needs no connection.
        PURE_SIM-resolved requires a topology card; DEVICE_SIM / LIVE-resolved
        requires both cards AND a live connection.
        """
        resolved = resolve_effective_mode_for_device(
            mode, self.topology_sim_override(),
        ).resolved
        if resolved is WorkflowRunMode.PURE_SIM:
            return self.topology_card is not None
        if self.topology_card is None or self.connection_card is None:
            return False
        return await self.is_client_connected()


@dataclass(frozen=True)
class DeviceUnionEntry:
    """Union view of a device across topology and gateway.

    `in_topology=True, gateway_connected=True` -> the happy path: a
    declared device is reachable. `in_topology=True, gateway_connected=False`
    -> declared but not yet connected (normal at boot). `in_topology=False,
    gateway_connected=True` -> the gateway connected with a name not declared
    in topology; surfaced so operators can spot misconfigurations.
    `topology_kind` and `gateway_kind` may differ when an operator has
    swapped a device class on-prem; that is logged as kind drift but
    accepted, so both labels surface here for operator visibility.
    """
    name: str
    in_topology: bool
    gateway_connected: bool
    last_heartbeat: datetime | None
    topology_kind: str | None
    gateway_kind: str | None
    interfaces: tuple[str, ...]
    position_ids: tuple[str, ...]
    status: str | None
    faulted: bool = False
    """Whether a command left this device part-way through something.

    A flag, not the record: one stop can fault several devices at once, and
    this is what says which ones to read. The fault itself (what command,
    what error, whether it may still be moving) is on the per-device status.
    """


@dataclass(frozen=True)
class TransporterSnapshot:
    name: str
    type_name: str
    is_busy: bool
    position_ids: tuple[str, ...]
    current_labware_id: str | None            # labware currently in the gripper (None if empty)
    # Mirror of ``DeviceSnapshot.under_external_control``: True when
    # a hosted device-integration gateway holds the transporter for ad-hoc
    # operator work (teach-point retake etc.). Orca move dispatch refuses
    # while set.
    under_external_control: bool = False
    external_control_hold: str | None = None
    """Why an operator is holding this, or None if nobody is.

    ``under_external_control`` is also true for the instant the gateway holds
    it around one ad-hoc command. This field is set only by the standing
    operator claim, which lasts until it is released; empty string means held
    with no reason given."""


@dataclass(frozen=True)
class MoverSnapshot:
    """Anything that can pick a plate up, including a liquid handler's own
    on-deck gripper.

    ``TransporterSnapshot`` covers the external arms only, because the things
    that consume it wire teachpoints and graph edges a device-owned gripper has
    neither of. This is the list for the operator question those miss:
    which of the machine's grippers is holding something right now.
    """
    name: str
    type_name: str
    is_busy: bool
    gripper_position_id: str
    current_labware_id: str | None
    under_external_control: bool = False
    external_control_hold: str | None = None
    """Why an operator is holding this, or None if nobody is.

    ``under_external_control`` is also true for the instant the gateway holds
    it around one ad-hoc command. This field is set only by the standing
    operator claim, which lasts until it is released; empty string means held
    with no reason given."""


@dataclass(frozen=True)
class ResourcePoolSnapshot:
    name: str
    member_names: tuple[str, ...]
    available_count: int                      # members that are not busy


# -- Templates (registry inventory) --------------------------------------


@dataclass(frozen=True)
class LabwareTemplateSnapshot:
    name: str
    type_name: str


@dataclass(frozen=True)
class MethodTemplateSnapshot:
    workflow_name: str
    name: str
    failure_policy: FailurePolicy


@dataclass(frozen=True)
class WorkflowTemplateSnapshot:
    name: str
    entry_thread_template_names: tuple[str, ...]


@dataclass(frozen=True)
class ThreadTemplateSnapshot:
    workflow_name: str
    name: str
    labware_template_name: str
    start_position_id: str
    end_position_ids: tuple[str, ...]


# -- System-level --------------------------------------------------------


@dataclass(frozen=True)
class SystemInfoSnapshot:
    name: str
    description: str
    version: str


@dataclass(frozen=True)
class PluginSnapshot:
    type_name: str
    disabled: bool
    exposed_command_names: tuple[str, ...]


# -- Device capability introspection -------------------------------------


@dataclass(frozen=True)
class SubmissionSnapshot:
    """Point-in-time view of a Submission.

    ``run_mode`` is the resolved ``WorkflowRunMode`` stamped on the
    underlying ``Submission`` at submit time. Operators / AI agents
    use this to confirm "this submission ran in PURE_SIM" without
    deriving from deployment defaults.
    """
    id: str
    execution_id: str
    workflow_name: str
    group_count: int
    status: SubmissionStatus
    batch_mode: BatchMode
    submitted_at: str                         # ISO-8601
    run_mode: WorkflowRunMode
    operator_id: str | None
    deployment_profile: str | None


@dataclass(frozen=True)
class SubmissionCloseResult:
    """Response from closing a batch/submission execution."""
    execution_id: str
    phase: ExecutionPhase


@dataclass(frozen=True)
class CommandDescriptor:
    """Advertised command on a device. One per capability method.

    `cli_accessible` is False when any parameter has a non-JSON-primitive
    type (e.g. `List[IWell]`); CLI refuses `device invoke` for those and
    points at `device.run_protocol` or the SDK instead.
    """
    device_name: str
    capability: str                           # e.g. "shaker.shake"
    danger_level: DangerLevel
    description: str
    cli_accessible: bool
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)


# Re-export from danger module for single-import ergonomics on the snapshot side.
__all__ = [
    "ActionSnapshot",
    "MethodSnapshot",
    "ThreadSnapshot",
    "ReservationSnapshot",
    "ExecutionDetail",
    "LabwareSnapshot",
    "LocationEvent",
    "LocationSnapshot",
    "DeviceSnapshot",
    "DeviceIntrospection",
    "TopologyDeviceEntry",
    "GatewayDeviceEntry",
    "DeviceUnionEntry",
    "TopologyCard",
    "ConnectionCard",
    "DeviceRegistryEntry",
    "WorkflowRunMode",
    "TransporterSnapshot",
    "ResourcePoolSnapshot",
    "LabwareTemplateSnapshot",
    "MethodTemplateSnapshot",
    "WorkflowTemplateSnapshot",
    "ThreadTemplateSnapshot",
    "SystemInfoSnapshot",
    "PluginSnapshot",
    "CommandDescriptor",
    "ActionDescriptor",
    "ParamSpec",
    "DangerLevel",
    "SubmissionSnapshot",
    "SubmissionCloseResult",
]
