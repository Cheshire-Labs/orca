"""Wire-forwarding `ITransporterDriver` for remote transporters.

Sibling of `Remote*Driver` in `remote_drivers.py` for the device side.
Constructed by `RemoteDeviceFactory._drivers_for_transporter` and
wrapped inside an orca-core `Transporter` with a paired
`SimTransporterDriver` so the `SimulationManager` can toggle between
sim + live per dispatch.

The driver is a pure wire-forwarder. orca-core's `Transporter.pick` /
`.place` resolve the destination teachpoint name + walk the gateway
chain BEFORE dispatching here; the driver receives a fully-resolved
`Teachpoint` plus an already-walked `gateway_path` list and forwards
the payload over the WebSocket without consulting any store.

Why this is its own module instead of a class in `remote_drivers.py`:
the `_RemoteDriverBase` shared base in `remote_drivers.py` declares a
parameterless `initialize()` for the `BaseDriver` hierarchy.
`ITransporterDriver.initialize` takes a typed `InitializeRequest`, an
LSP-incompatible signature. Inheriting `_RemoteDriverBase` would
shadow the typed signature; we keep `RemoteTransporterDriver`
self-contained instead.
"""

import logging
from typing import ClassVar, Dict

from pydantic import JsonValue

from cheshire_drivers.interfaces import ITransporterDriver
from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from cheshire_drivers.teachpoints import CartesianCoordinates, JointCoordinates
from cheshire_drivers.homing_models import HomeRequest
from cheshire_drivers.transporter_models import (
    CloseGripperRequest,
    EnsureSeededRequest,
    GetCartesianPositionRequest,
    GetJointPositionRequest,
    GetSpeedRequest,
    HaltRequest,
    InitializeRequest,
    MoveSingleAxisRelativeRequest,
    MoveSingleAxisRequest,
    MoveToCoordsRequest,
    MoveToSafeRequest,
    OpenGripperRequest,
    PickAtCoordsRequest,
    PlaceAtCoordsRequest,
    ResetWorldRequest,
    SeedPositionRequest,
    SetFreeModeRequest,
    SetSpeedRequest,
    SpeedResponse,
    UnseedPositionRequest,
)

from orca.runtime.run_modes import current_execution_id

from orca.gateway.controller.command_kind import CommandKind
from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_drivers import ModeResolver

logger = logging.getLogger(__name__)


class RemoteTransporterDriver(GatewayBackedDriver, ITransporterDriver):
    """`ITransporterDriver` whose every method forwards over the wire.

    Each dispatch builds a flat-dict payload and calls
    `DeviceController.execute_command(device_id=name, command=...,
    params=..., effective_mode=resolver(name))`. The `effective_mode`
    is resolved per dispatch so the driver picks up topology
    `sim_override` changes without rebuilding.

    For pick / place / move_to_coords the driver expects a
    fully-resolved `Teachpoint` (and optional `gateway_path`) on the
    request -- the orca-core `Transporter` does the server-side
    resolution before dispatching.

    ``resend_on_reconnect``: transporter commands are safe to
    resend after a transient WS drop. A pick or place that didn't
    reach the device leaves the robot idle; an in-flight motion that
    dropped mid-execution is interruptible (gripper state survives
    safely). Default ``True`` preserves the gateway's transparent
    resend behavior. Liquid handlers override to ``False`` because
    aspirate / dispense cannot be safely re-executed.
    """

    interfaces: ClassVar[frozenset[str]] = frozenset({"ITransporter", "IHomeable"})
    resend_on_reconnect: ClassVar[bool] = True

    def __init__(
        self,
        name: str,
        controller: DeviceController,
        mode_resolver: ModeResolver,
        timeout: float = 30.0,
        *,
        declared_interfaces: frozenset[str] | None = None,
    ) -> None:
        super().__init__(declared_interfaces)
        self._name = name
        self._controller = controller
        self._mode_resolver = mode_resolver
        self._timeout = timeout
        # Cached link flags, and their expiry, exactly as
        # `_RemoteDriverBase` in remote_drivers.py describes them.
        self._initialized = False
        self._connected = False

    @property
    def name(self) -> str:
        return self._name


    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_connected(self) -> bool:
        """Whether the arm's own link to its hardware is open. See `_RemoteDriverBase`."""
        return self._connected

    def forget_driver_session(self) -> None:
        """Stop answering for a device bridge session that is over.

        Sibling of `_RemoteDriverBase.forget_driver_session`; see it for the
        why. It matters most here: `Transporter.ensure_initialized` reads
        `is_initialized` before every pick, so a flag that outlives its session
        is what sends an arm on a move it was never brought up or homed for.
        """
        self._initialized = False
        self._connected = False

    async def _send(
        self,
        command: str,
        params: Dict[str, JsonValue] | None = None,
        kind: CommandKind = CommandKind.ACTUATION,
    ) -> JsonValue:
        effective_mode = self._mode_resolver(self._name)
        return await self._controller.execute_command(
            device_id=self._name,
            command=command,
            params=dict(params) if params is not None else {},
            timeout_seconds=self._timeout,
            effective_mode=effective_mode,
            resend_on_reconnect=type(self).resend_on_reconnect,
            kind=kind,
            execution_id=current_execution_id.get(),
        )

    @staticmethod
    def _coords_payload(
        request: PickAtCoordsRequest | PlaceAtCoordsRequest,
    ) -> Dict[str, JsonValue]:
        """Flatten a pick/place teachpoint-coords Request into the wire dict.

        Used by `pick_at_coords` and `place_at_coords` only. `move_to_coords`
        sends a smaller payload (just `teachpoint`) and is NOT routed through
        this helper -- the wire shape is genuinely different because
        `MoveToCoordsRequest` does not carry `labware_type`,
        `gateway_path`, or `expected_labware` (its `_StrictModel` base would
        reject them).

        The device bridge's `wrap_transporter_payload` runs the dict through
        `PickAtCoordsRequest.model_validate`; the field validators on
        the Request materialize each `Teachpoint` back from its flat
        `to_dict()` form and the `expected_labware` LabwareIdentity from
        its dict form.
        """
        return {
            "teachpoint": request.teachpoint.to_dict(),
            "labware_type": request.labware_type,
            "gateway_path": [wp.to_dict() for wp in request.gateway_path],
            "expected_labware": (
                request.expected_labware.model_dump()
                if request.expected_labware is not None
                else None
            ),
            "handling": request.handling.model_dump(),
        }

    # -- Lifecycle --

    async def initialize(self, request: InitializeRequest) -> None:
        await self._send("initialize", request.model_dump(exclude_none=True))
        self._connected = True
        self._initialized = True

    async def connect(self) -> None:
        await self._send("connect", {})
        self._connected = True

    async def disconnect(self) -> None:
        await self._send("disconnect", {})
        self._connected = False
        self._initialized = False

    async def home(self, request: HomeRequest) -> None:
        await self._send("home", request.model_dump(exclude_none=True))

    async def move_to_safe(self, request: MoveToSafeRequest) -> None:
        await self._send("move_to_safe", request.model_dump(exclude_none=True))

    async def halt(self, request: HaltRequest) -> None:
        await self._send("halt", request.model_dump(exclude_none=True))

    # -- Pick / place / move (cloud-resolved teachpoints) --

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        await self._send("pick_at_coords", self._coords_payload(request))

    async def place_at_coords(self, request: PlaceAtCoordsRequest) -> None:
        await self._send("place_at_coords", self._coords_payload(request))

    async def move_to_coords(self, request: MoveToCoordsRequest) -> None:
        await self._send("move_to_coords", {"teachpoint": request.teachpoint.to_dict()})

    # -- Single-axis moves --

    async def move_single_axis(self, request: MoveSingleAxisRequest) -> None:
        await self._send("move_single_axis", request.model_dump(exclude_none=True))

    async def move_single_axis_relative(
        self, request: MoveSingleAxisRelativeRequest,
    ) -> None:
        await self._send("move_single_axis_relative", request.model_dump(exclude_none=True))

    # -- Gripper / mode / speed --

    async def open_gripper(self, request: OpenGripperRequest) -> None:
        await self._send("open_gripper", request.model_dump(exclude_none=True))

    async def close_gripper(self, request: CloseGripperRequest) -> None:
        await self._send("close_gripper", request.model_dump(exclude_none=True))

    async def set_free_mode(self, request: SetFreeModeRequest) -> None:
        await self._send("set_free_mode", request.model_dump(exclude_none=True))

    async def set_speed(self, request: SetSpeedRequest) -> None:
        await self._send("set_speed", request.model_dump(exclude_none=True))

    async def get_speed(self, request: GetSpeedRequest) -> float:
        result = await self._send("get_speed", request.model_dump(exclude_none=True))
        return SpeedResponse.model_validate(result).speed

    # -- Position queries --

    async def get_joint_position(
        self, request: GetJointPositionRequest,
    ) -> JointCoordinates:
        result = await self._send(
            "get_joint_position", request.model_dump(exclude_none=True),
        )
        return JointCoordinates.model_validate(result)

    async def get_cartesian_position(
        self, request: GetCartesianPositionRequest,
    ) -> CartesianCoordinates:
        result = await self._send(
            "get_cartesian_position", request.model_dump(exclude_none=True),
        )
        return CartesianCoordinates.model_validate(result)


    # -- World-state sync wire ops (workflow-internal) --
    #
    # Forward a `LabwareIdentity` + position_id over the wire so the device
    # bridge's transporter sim graph can stay in sync with server-side labware
    # tracking. Real-hardware drivers (PLR-backed) treat these as no-ops; the
    # sim wrapper consumes them.
    #
    # These carry ``CommandKind.WORLD_SYNC``: they update the driver's view of
    # the world rather than moving anything, so they must not queue behind a
    # workflow command or raise ``DeviceLockedError`` racing one.

    @staticmethod
    def _world_payload(
        request: SeedPositionRequest | EnsureSeededRequest | UnseedPositionRequest,
    ) -> Dict[str, JsonValue]:
        return {
            "position_id": request.position_id,
            "labware": request.labware.model_dump(),
        }

    async def seed_position(self, request: SeedPositionRequest) -> None:
        await self._send(
            "seed_position", self._world_payload(request), kind=CommandKind.WORLD_SYNC,
        )

    async def ensure_seeded(self, request: EnsureSeededRequest) -> None:
        await self._send(
            "ensure_seeded", self._world_payload(request), kind=CommandKind.WORLD_SYNC,
        )

    async def unseed_position(self, request: UnseedPositionRequest) -> None:
        await self._send(
            "unseed_position", self._world_payload(request), kind=CommandKind.WORLD_SYNC,
        )

    async def reset_world(self, request: ResetWorldRequest) -> None:
        # Blanket world wipe carries no position/labware -- send an empty
        # payload, not `_world_payload` (which requires position_id + labware).
        await self._send("reset_world", {}, kind=CommandKind.WORLD_SYNC)


class RemoteTranslatorDriver(RemoteTransporterDriver):
    """Remote proxy for a translator: one carriage serving every position.

    The proxy cannot ask the instrument how many carriages it has, and the
    answer is needed at topology-build time -- before any device bridge has
    connected. It comes from the deployment declaring a `Translator`, which is
    what selects this driver.
    """

    @property
    def single_carriage(self) -> bool:
        return True


__all__ = ["RemoteTransporterDriver", "RemoteTranslatorDriver"]
