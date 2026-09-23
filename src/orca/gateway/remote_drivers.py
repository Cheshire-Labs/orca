"""Remote drivers that conform to cheshire-drivers driver interfaces.

Each `Remote*Driver` is a thin adapter that:
  1. Implements a cheshire-drivers `I*Driver` interface (taking Pydantic
     Request models on every method).
  2. Forwards each call through `DeviceController.execute_command` over
     the device-integration WebSocket.
  3. Carries an `effective_mode` per dispatch so the device bridge can pick
     between the configured live backend (LIVE) and a Sim* class
     (DEVICE_SIM).

These drivers are supplied by `RemoteDeviceFactory.build_drivers` and
wrapped inside an orca-core `Shaker` / `Centrifuge` / etc. with a paired
in-process `Sim*Driver`. The orca-core `SimulationManager` decides which
of the two to actually call per dispatch (sim vs live); when the live path
is taken, this module forwards over the wire. This is the production wire-
forwarding path: customer topology code authors plain orca-core devices,
and the bound `RemoteDeviceFactory` supplies the paired (Remote*Driver,
Sim*Driver) so the SimulationManager-aware `Device` machinery sees a
uniform contract on both the live and sim sides.
"""

import logging
from typing import Callable, ClassVar, Dict

from pydantic import JsonValue

from cheshire_drivers.centrifuge_models import CentrifugeRequest
from cheshire_drivers.command_responses import TemperatureResponse
from cheshire_drivers.delidder_models import DelidRequest
from cheshire_drivers.gantry_models import ParkGantryRequest
from cheshire_drivers.gripper_models import MoveGripperToRequest
from cheshire_drivers.interfaces import (
    ICentrifugeDriver,
    IDelidderDriver,
    ILiquidHandlerDriver,
    IPlateWasherDriver,
    IProtocolRunnerDriver,
    IReaderDriver,
    ISealerDriver,
    IShakerDriver,
    IThermocyclerDriver,
)
from cheshire_drivers.liquid_handler_models import (
    AspirateRequest,
    Aspirate96Request,
    DeckLayoutConfig,
    DeckStateResponse,
    DiscardTipsRequest,
    DispenseRequest,
    Dispense96Request,
    DropTipsRequest,
    DropTips96Request,
    GetDeckStateRequest,
    GetHeadConfigurationRequest,
    HeadConfigurationResponse,
    LabwareStateResponse,
    MixRequest,
    MovePlateRequest,
    PickUpTipsRequest,
    PickUpTips96Request,
    AddDeckLabwareRequest,
    DiscardStrandedTipsRequest,
    ReconcileDeckOccupancyRequest,
    ReconcileHardwareStateRequest,
    ReconcileHardwareStateResponse,
    ResetDeckLabwareRequest,
    ReturnTips96Request,
    RemoveDeckLabwareRequest,
)
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from cheshire_drivers.reader_models import ReadRequest
from cheshire_drivers.sealer_models import SealRequest
from cheshire_drivers.shaker_models import (
    LockPlateRequest,
    ShakeRequest,
    StopShakingRequest,
    UnlockPlateRequest,
)
from cheshire_drivers.thermocycler_models import (
    CloseLidRequest,
    CycleCountResponse,
    CycleIndexResponse,
    DeactivateBlockRequest,
    DeactivateLidRequest,
    GetBlockCurrentTemperatureRequest,
    GetBlockStatusRequest,
    GetBlockTargetTemperatureRequest,
    GetCurrentCycleIndexRequest,
    GetCurrentStepIndexRequest,
    GetHoldTimeRequest,
    GetLidCurrentTemperatureRequest,
    GetLidOpenRequest,
    GetLidStatusRequest,
    GetLidTargetTemperatureRequest,
    GetTotalCycleCountRequest,
    GetTotalStepCountRequest,
    HoldTimeResponse,
    LidOpenResponse,
    OpenLidRequest,
    RunProtocolRequest as ThermocyclerRunProtocolRequest,
    SetBlockTemperatureRequest,
    SetLidTemperatureRequest,
    StepCountResponse,
    StepIndexResponse,
    TemperatureListResponse,
    ThermocyclerStatusResponse,
)

from orca.runtime.run_modes import WorkflowRunMode, current_execution_id

from orca.gateway.controller.controller import DeviceController
from orca.gateway.gateway_backed_driver import GatewayBackedDriver

logger = logging.getLogger(__name__)


# Resolver shape: the factory hands one callable to every driver it builds.
# At dispatch time the driver calls resolver(device_name) to learn the
# effective_mode for THIS command. The resolver is a closure over the
# per-task ``current_run_mode`` ContextVar + per-device topology
# sim_override; updates to those inputs surface immediately through the
# resolver without rebinding the driver.
ModeResolver = Callable[[str], WorkflowRunMode]


class _RemoteDriverBase(GatewayBackedDriver):
    """Common state + send path shared by every Remote*Driver.

    Holds the device name, the controller, the per-device timeout, and the
    factory-supplied mode resolver. `_send` builds the wire payload (flat
    `params` dict, plus the resolved `effective_mode`) and awaits the
    controller; subclasses provide the typed methods that call `_send`.

    ``resend_on_reconnect`` is the class-level disconnect-recovery policy.
    Default ``True`` keeps the gateway's legacy transparent-resend
    behavior, which is safe for shakers / centrifuges / sealers / readers
    / etc. -- a transient WS drop on those drivers almost always means
    the device wasn't reached, so resending after reconnect is the
    correct recovery. Subclasses whose commands are non-idempotent
    override to ``False`` (see :class:`RemoteLiquidHandlerDriver`); the
    controller then fails the pending future with ``DeviceOfflineError``
    on reconnect instead of resending.
    """

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
        # Cached so `Device.is_initialized` answers without a wire round trip on
        # the hot path of a move; good only for the session that earned it.
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
        """Whether this device's own link to its hardware is open.

        Cached the same way as `is_initialized`, and expires the same way.
        Tracked separately because the two verbs move independently -- a device
        can be linked but not brought up, and `initialize` opens the link on
        its way through.
        """
        return self._connected

    def forget_driver_session(self) -> None:
        """Stop answering for a device bridge session that is over.

        A restarted device bridge builds fresh driver objects, so nothing
        behind the wire is linked or brought up however recently orca watched
        either succeed. Pushed in when the link drops, returns, or the device
        bridge reports otherwise, rather than pulled on read, because the read
        is synchronous and sits on the hot path of every move.
        """
        self._connected = False
        self._initialized = False

    async def _send(
        self,
        command: str,
        params: Dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        effective_mode = self._mode_resolver(self._name)
        return await self._controller.execute_command(
            device_id=self._name,
            command=command,
            params=dict(params) if params is not None else {},
            timeout_seconds=self._timeout,
            effective_mode=effective_mode,
            resend_on_reconnect=type(self).resend_on_reconnect,
            execution_id=current_execution_id.get(),
        )

    # -- BaseDriver lifecycle (parameterless across all device categories) --

    async def initialize(self) -> None:
        await self._send("initialize")
        self._connected = True
        self._initialized = True

    async def connect(self) -> None:
        await self._send("connect")
        self._connected = True

    async def disconnect(self) -> None:
        await self._send("disconnect")
        self._connected = False
        self._initialized = False

    async def open(self) -> None:
        await self._send("open")

    async def close(self) -> None:
        await self._send("close")


class RemoteShakerDriver(_RemoteDriverBase, IShakerDriver):
    """`IShakerDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"IShaker"})

    @property
    def supports_locking(self) -> bool:
        # ``IShakerDriver`` declares ``supports_locking`` abstract so every
        # concrete subclass must answer it. Hard-coding ``True`` here is a
        # safe-default for the wire forwarder because no orca-core code
        # path consumes this property today (verified by grep across
        # orca-core/src — no consumers). The truth lives on the
        # device bridge's backend; once a wire query (or
        # ``DeviceConnectInfo`` carry-through) is wired up, replace this
        # constant. Until then, ``True`` is the conservative answer:
        # callers that would gate behavior on this property will attempt
        # the lock_plate / unlock_plate path, which the wire forwarder
        # will route to the device bridge and surface real errors from
        # backends that do not implement it.
        return True

    async def stop(self) -> None:
        await self._send("stop")

    async def shake(self, request: ShakeRequest) -> None:
        await self._send("shake", request.model_dump(exclude_none=True))

    async def stop_shaking(self, request: StopShakingRequest) -> None:
        await self._send("stop_shaking", request.model_dump(exclude_none=True))

    async def lock_plate(self, request: LockPlateRequest) -> None:
        await self._send("lock_plate", request.model_dump(exclude_none=True))

    async def unlock_plate(self, request: UnlockPlateRequest) -> None:
        await self._send("unlock_plate", request.model_dump(exclude_none=True))


class RemoteCentrifugeDriver(_RemoteDriverBase, ICentrifugeDriver):
    """`ICentrifugeDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"ICentrifuge"})

    async def centrifuge(self, request: CentrifugeRequest) -> None:
        await self._send("centrifuge", request.model_dump(exclude_none=True))


class RemoteThermocyclerDriver(_RemoteDriverBase, IThermocyclerDriver):
    """`IThermocyclerDriver` whose every method forwards over the wire.

    Mutations mirror `RemoteCentrifugeDriver`; getters rehydrate the typed
    Response model from the wire dict (mirror `RemoteTransporterDriver.
    get_joint_position`). The orca-core `Thermocycler` unwraps the semantic
    value from each Response after this driver returns it.
    """

    interfaces: ClassVar[frozenset[str]] = frozenset({"IThermocycler"})

    async def open_lid(self, request: OpenLidRequest) -> None:
        await self._send("open_lid", request.model_dump(exclude_none=True))

    async def close_lid(self, request: CloseLidRequest) -> None:
        await self._send("close_lid", request.model_dump(exclude_none=True))

    async def set_block_temperature(self, request: SetBlockTemperatureRequest) -> None:
        await self._send("set_block_temperature", request.model_dump(exclude_none=True))

    async def set_lid_temperature(self, request: SetLidTemperatureRequest) -> None:
        await self._send("set_lid_temperature", request.model_dump(exclude_none=True))

    async def deactivate_block(self, request: DeactivateBlockRequest) -> None:
        await self._send("deactivate_block", request.model_dump(exclude_none=True))

    async def deactivate_lid(self, request: DeactivateLidRequest) -> None:
        await self._send("deactivate_lid", request.model_dump(exclude_none=True))

    async def run_protocol(self, request: ThermocyclerRunProtocolRequest) -> None:
        await self._send("run_protocol", request.model_dump(exclude_none=True))

    async def get_block_current_temperature(
        self, request: GetBlockCurrentTemperatureRequest,
    ) -> TemperatureListResponse:
        result = await self._send(
            "get_block_current_temperature", request.model_dump(exclude_none=True),
        )
        return TemperatureListResponse.model_validate(result)

    async def get_block_target_temperature(
        self, request: GetBlockTargetTemperatureRequest,
    ) -> TemperatureListResponse:
        result = await self._send(
            "get_block_target_temperature", request.model_dump(exclude_none=True),
        )
        return TemperatureListResponse.model_validate(result)

    async def get_lid_current_temperature(
        self, request: GetLidCurrentTemperatureRequest,
    ) -> TemperatureListResponse:
        result = await self._send(
            "get_lid_current_temperature", request.model_dump(exclude_none=True),
        )
        return TemperatureListResponse.model_validate(result)

    async def get_lid_target_temperature(
        self, request: GetLidTargetTemperatureRequest,
    ) -> TemperatureListResponse:
        result = await self._send(
            "get_lid_target_temperature", request.model_dump(exclude_none=True),
        )
        return TemperatureListResponse.model_validate(result)

    async def get_lid_open(self, request: GetLidOpenRequest) -> LidOpenResponse:
        result = await self._send("get_lid_open", request.model_dump(exclude_none=True))
        return LidOpenResponse.model_validate(result)

    async def get_lid_status(
        self, request: GetLidStatusRequest,
    ) -> ThermocyclerStatusResponse:
        result = await self._send("get_lid_status", request.model_dump(exclude_none=True))
        return ThermocyclerStatusResponse.model_validate(result)

    async def get_block_status(
        self, request: GetBlockStatusRequest,
    ) -> ThermocyclerStatusResponse:
        result = await self._send("get_block_status", request.model_dump(exclude_none=True))
        return ThermocyclerStatusResponse.model_validate(result)

    async def get_hold_time(self, request: GetHoldTimeRequest) -> HoldTimeResponse:
        result = await self._send("get_hold_time", request.model_dump(exclude_none=True))
        return HoldTimeResponse.model_validate(result)

    async def get_current_cycle_index(
        self, request: GetCurrentCycleIndexRequest,
    ) -> CycleIndexResponse:
        result = await self._send(
            "get_current_cycle_index", request.model_dump(exclude_none=True),
        )
        return CycleIndexResponse.model_validate(result)

    async def get_total_cycle_count(
        self, request: GetTotalCycleCountRequest,
    ) -> CycleCountResponse:
        result = await self._send(
            "get_total_cycle_count", request.model_dump(exclude_none=True),
        )
        return CycleCountResponse.model_validate(result)

    async def get_current_step_index(
        self, request: GetCurrentStepIndexRequest,
    ) -> StepIndexResponse:
        result = await self._send(
            "get_current_step_index", request.model_dump(exclude_none=True),
        )
        return StepIndexResponse.model_validate(result)

    async def get_total_step_count(
        self, request: GetTotalStepCountRequest,
    ) -> StepCountResponse:
        result = await self._send(
            "get_total_step_count", request.model_dump(exclude_none=True),
        )
        return StepCountResponse.model_validate(result)


class RemoteSealerDriver(_RemoteDriverBase, ISealerDriver):
    """`ISealerDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"ISealer"})

    async def seal(self, request: SealRequest) -> None:
        await self._send("seal", request.model_dump(exclude_none=True))

    async def set_temperature(self, temperature: float) -> None:
        await self._send("set_temperature", {"temperature": temperature})

    async def get_temperature(self) -> float:
        result = await self._send("get_temperature")
        return TemperatureResponse.model_validate(result).temperature


class RemoteReaderDriver(_RemoteDriverBase, IReaderDriver):
    """`IReaderDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"IReader"})

    async def read(self, request: ReadRequest) -> None:
        await self._send("read", request.model_dump(exclude_none=True))


class RemoteDelidderDriver(_RemoteDriverBase, IDelidderDriver):
    """`IDelidderDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"IDelidder"})

    async def delid(self, request: DelidRequest) -> None:
        await self._send("delid", request.model_dump(exclude_none=True))


class RemoteProtocolRunnerDriver(_RemoteDriverBase, IProtocolRunnerDriver):
    """`IProtocolRunnerDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"IProtocolRunner"})

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        await self._send("run_protocol", request.model_dump(exclude_none=True))


class RemotePlateWasherDriver(_RemoteDriverBase, IPlateWasherDriver):
    """`IPlateWasherDriver` whose every method forwards over the wire.

    PlateWasher inherits from `IProtocolRunnerDriver`, so the only abstract
    method is `run_protocol`.
    """

    interfaces: ClassVar[frozenset[str]] = frozenset(
        {"IPlateWasher", "IProtocolRunner"}
    )

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        await self._send("run_protocol", request.model_dump(exclude_none=True))


class GantryParkingNotSupportedError(RuntimeError):
    """Raised when a handler with no way to park is asked to get out of the way."""


class GripperMotionNotSupportedError(RuntimeError):
    """Raised when a gripper move is asked of a handler with no gripper."""


class RunProtocolNotSupportedError(RuntimeError):
    """Raised when `run_protocol` is called on a plr-only liquid handler.

    Mirrors orca-core `LiquidHandler.run_protocol`'s fail-fast intent at the
    wire-driver boundary: a device whose advertised profile omits
    `IProtocolRunner` has no protocol-execution capability, so the call is
    rejected before any wire dispatch rather than forwarded to a device bridge
    that would reject it.
    """


class _RemoteLiquidHandlerWireMixin(_RemoteDriverBase, ILiquidHandlerDriver):
    """Atomic-op wire forwarders shared by every LH profile.

    Driver-level signature shape: every method takes a Pydantic Request model,
    exactly matching `ILiquidHandlerDriver`. The orca-core `LiquidHandler`
    bridge does its own grouping + Request construction and then calls these
    methods, so this class never sees `IWell` objects -- it just dumps the
    Request and forwards.

    `provides_state=True`: PLR-driven liquid handlers report reliable
    LabwareStateResponse on every atomic call. The orca-core `LiquidHandler`
    bridge reads this flag to decide whether to emit DRIVER_OBSERVED
    tracking; passing it through here keeps the wire-driver path consistent
    with the in-process driver path.

    `resend_on_reconnect=False`: liquid-handler commands are not
    idempotent. A transient WebSocket drop during a mid-channel
    aspirate / dispense / tip operation cannot be safely resent on
    reconnect -- the on-prem driver may have already executed the
    operation, and a resend would double-execute on the physical
    device. The controller fails the pending future with
    ``DeviceOfflineError`` instead, forcing the operator to recover
    manually. Deduplicating command_id on the device bridge is the universal
    architectural fix; until that lands, this opt-out is the
    safest behavior for LH dispatches.

    The three concrete profile classes below differ only in their
    `interfaces` ClassVar (which the orca facade + connect-time superset
    check read off `type(driver)`) and in whether `run_protocol` forwards or
    fails fast. The wire surface is uniform: the device bridge routes whatever
    command arrives.
    """

    provides_state: ClassVar[bool] = True
    resend_on_reconnect: ClassVar[bool] = False

    async def configure_deck(self, config: DeckLayoutConfig) -> LabwareStateResponse:
        result = await self._send("configure_deck", config.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def get_deck_state(self, request: GetDeckStateRequest) -> DeckStateResponse:
        result = await self._send("get_deck_state", request.model_dump(exclude_none=True))
        return DeckStateResponse.model_validate(result)

    async def get_head_configuration(
        self, request: GetHeadConfigurationRequest
    ) -> HeadConfigurationResponse:
        result = await self._send("get_head_configuration", request.model_dump(exclude_none=True))
        return HeadConfigurationResponse.model_validate(result)

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        result = await self._send("aspirate", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def dispense(self, request: DispenseRequest) -> LabwareStateResponse:
        result = await self._send("dispense", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def pick_up_tips(self, request: PickUpTipsRequest) -> LabwareStateResponse:
        result = await self._send("pick_up_tips", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def drop_tips(self, request: DropTipsRequest) -> LabwareStateResponse:
        result = await self._send("drop_tips", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def discard_tips(self, request: DiscardTipsRequest) -> LabwareStateResponse:
        result = await self._send("discard_tips", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def mix(self, request: MixRequest) -> LabwareStateResponse:
        result = await self._send("mix", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def aspirate96(self, request: Aspirate96Request) -> LabwareStateResponse:
        result = await self._send("aspirate96", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def dispense96(self, request: Dispense96Request) -> LabwareStateResponse:
        result = await self._send("dispense96", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def pick_up_tips96(self, request: PickUpTips96Request) -> LabwareStateResponse:
        result = await self._send("pick_up_tips96", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def drop_tips96(self, request: DropTips96Request) -> LabwareStateResponse:
        result = await self._send("drop_tips96", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def return_tips96(self, request: ReturnTips96Request) -> LabwareStateResponse:
        result = await self._send("return_tips96", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def move_plate(self, request: MovePlateRequest) -> None:
        await self._send("move_plate", request.model_dump(exclude_none=True))

    async def park_gantry(self, request: ParkGantryRequest) -> None:
        declared = self._declared_interfaces
        if declared is not None and "IGantryParking" not in declared:
            raise GantryParkingNotSupportedError(
                f"Device {self._name!r} advertises {sorted(declared)!r} and has "
                f"no IGantryParking capability; park_gantry is not supported."
            )
        await self._send("park_gantry", request.model_dump(exclude_none=True))

    async def move_gripper_to(self, request: MoveGripperToRequest) -> None:
        # Gated on the advertised card, not just declared here: the wire surface
        # is uniform across LH profiles, so without this every remote handler
        # would satisfy a structural "can you move your gripper" probe and a
        # gripperless one would only fail once the command reached the device.
        declared = self._declared_interfaces
        if declared is not None and "IGripperMotion" not in declared:
            raise GripperMotionNotSupportedError(
                f"Device {self._name!r} advertises {sorted(declared)!r} and has "
                f"no IGripperMotion capability; move_gripper_to is not supported."
            )
        await self._send("move_gripper_to", request.model_dump(exclude_none=True))

    async def add_deck_labware(self, request: AddDeckLabwareRequest) -> None:
        await self._send("add_deck_labware", request.model_dump(exclude_none=True))

    async def remove_deck_labware(self, request: RemoveDeckLabwareRequest) -> None:
        await self._send("remove_deck_labware", request.model_dump(exclude_none=True))

    async def reset_deck_labware(self, request: ResetDeckLabwareRequest) -> LabwareStateResponse:
        result = await self._send("reset_deck_labware", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def reconcile_deck_occupancy(
        self, request: ReconcileDeckOccupancyRequest,
    ) -> LabwareStateResponse:
        result = await self._send("reconcile_deck_occupancy", request.model_dump(exclude_none=True))
        return LabwareStateResponse.model_validate(result)

    async def reconcile_hardware_state(
        self, request: ReconcileHardwareStateRequest,
    ) -> ReconcileHardwareStateResponse:
        result = await self._send("reconcile_hardware_state", request.model_dump(exclude_none=True))
        return ReconcileHardwareStateResponse.model_validate(result)

    async def discard_stranded_tips(
        self, request: DiscardStrandedTipsRequest,
    ) -> ReconcileHardwareStateResponse:
        result = await self._send("discard_stranded_tips", request.model_dump(exclude_none=True))
        return ReconcileHardwareStateResponse.model_validate(result)

    async def _forward_run_protocol(self, request: RunProtocolRequest) -> None:
        await self._send("run_protocol", request.model_dump(exclude_none=True))


class RemoteLiquidHandlerDriver(_RemoteLiquidHandlerWireMixin):
    """plr-only liquid handler (advertises `{ILiquidHandler}`).

    A pure-PLR backend (Opentrons, generic PLR LH) and the cold-start default
    before any device bridge has advertised a richer profile. Has no protocol
    capability, so `run_protocol` fails fast at the boundary.
    """

    interfaces: ClassVar[frozenset[str]] = frozenset({"ILiquidHandler"})

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        raise RunProtocolNotSupportedError(
            f"Device {self._name!r} advertises {sorted(self.interfaces)!r} and "
            f"has no IProtocolRunner capability; run_protocol is not supported."
        )


class RemoteLiquidHandlerWithProtocolDriver(_RemoteLiquidHandlerWireMixin):
    """plr + protocol liquid handler (advertises `{ILiquidHandler, IProtocolRunner}`).

    A Hamilton ML STAR + Venus, an Agilent Bravo + VWorks with PLR atomic ops,
    etc. Supports both atomic ops and vendor protocol files.
    """

    interfaces: ClassVar[frozenset[str]] = frozenset(
        {"ILiquidHandler", "IProtocolRunner"}
    )

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        await self._forward_run_protocol(request)


class RemoteProtocolOnlyLiquidHandlerDriver(_RemoteLiquidHandlerWireMixin):
    """protocol-only liquid handler (advertises `{IProtocolRunner}`).

    An Agilent Bravo + VWorks driven exclusively by vendor protocol files (no
    orca-level atomic ops; the orca-core `examples/smc_assay` bravo). The orca
    device is still a `LiquidHandler` (so the live driver implements the atomic
    abstracts), but the advertised set is `{IProtocolRunner}` only: the
    connect-time superset check and the operator surface see protocol
    execution, and the controller rejects atomic ops (declared ∩ advertised
    excludes `ILiquidHandler`).
    """

    interfaces: ClassVar[frozenset[str]] = frozenset({"IProtocolRunner"})

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        await self._forward_run_protocol(request)


__all__ = [
    "ModeResolver",
    "RemoteCentrifugeDriver",
    "RemoteDelidderDriver",
    "RemoteLiquidHandlerDriver",
    "RemoteLiquidHandlerWithProtocolDriver",
    "RemotePlateWasherDriver",
    "RemoteProtocolOnlyLiquidHandlerDriver",
    "RemoteProtocolRunnerDriver",
    "RemoteReaderDriver",
    "RemoteSealerDriver",
    "RemoteShakerDriver",
    "RemoteThermocyclerDriver",
    "RunProtocolNotSupportedError",
]
