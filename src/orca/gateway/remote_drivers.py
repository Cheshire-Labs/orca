"""Drivers that implement the cheshire-drivers interfaces by forwarding every call to the device's orca-client.

Each `Remote*Driver` sends its command through `IDeviceController.execute_command`,
stamped with the `effective_mode` the device dispatches under, and orca-client runs it
on the real driver (LIVE) or the device's simulator (SIM). A check run sends the same
commands to an orca-client inside Orca. `RemoteDeviceFactory` builds them, one per device.
"""

import logging
from typing import Awaitable, Callable, ClassVar, Dict

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
    ILiquidHandlerWithProtocolDriver,
    IPlateWasherDriver,
    IProtocolRunnerDriver,
    IReaderDriver,
    ISealerDriver,
    IShakerDriver,
    IStorageDriver,
    IThermocyclerDriver,
    IWasteDriver,
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
from cheshire_drivers.protocol_runner_models import LabwareHandoffRequest, RunProtocolRequest
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

from orca.modes.run_modes import WorkflowRunMode, current_execution_id

from orca.gateway.controller.controller import IDeviceController
from orca.gateway.gateway_backed_driver import GatewayBackedDriver

logger = logging.getLogger(__name__)


# The driver awaits resolver(device_name) for each command's effective_mode. It reads the run mode in force and
# the device's setting from the database, so a change to either reaches the next command.
ModeResolver = Callable[[str], Awaitable[WorkflowRunMode]]


class _RemoteDriverBase(GatewayBackedDriver):
    """Common state + send path shared by every Remote*Driver.

    Holds the device name, the controller, and the
    factory-supplied mode resolver. `_send` builds the wire payload (flat
    `params` dict, plus the resolved `effective_mode`) and awaits the
    controller; subclasses provide the typed methods that call `_send`.
    """

    def __init__(
        self,
        name: str,
        controller: IDeviceController,
        mode_resolver: ModeResolver,
        *,
        declared_interfaces: frozenset[str] | None = None,
    ) -> None:
        super().__init__(declared_interfaces)
        self._name = name
        self._controller = controller
        self._mode_resolver = mode_resolver
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
        effective_mode = await self._mode_resolver(self._name)
        return await self._controller.execute_command(
            device_id=self._name,
            command=command,
            params=dict(params) if params is not None else {},
            # The controller uses the command's declared limit (initialize, a pick): a fixed one cut motion off.
            timeout_seconds=None,
            effective_mode=effective_mode,
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
        # orca-core/src -- no consumers). The truth lives on the
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


class RemoteStorageDriver(_RemoteDriverBase, IStorageDriver):
    """`IStorageDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"IStorage"})

    async def dispense(self) -> None:
        await self._send("dispense", {})


class RemoteWasteDriver(RemoteStorageDriver, IWasteDriver):
    """`IWasteDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"IWaste", "IStorage"})


class _ForwardsLabwareHandoff(_RemoteDriverBase, IProtocolRunnerDriver):
    """Forwards the four labware handoff hooks over the wire."""

    async def prepare_for_place(self, request: LabwareHandoffRequest) -> None:
        await self._send("prepare_for_place", request.model_dump(exclude_none=True))

    async def notify_placed(self, request: LabwareHandoffRequest) -> None:
        await self._send("notify_placed", request.model_dump(exclude_none=True))

    async def prepare_for_pick(self, request: LabwareHandoffRequest) -> None:
        await self._send("prepare_for_pick", request.model_dump(exclude_none=True))

    async def notify_picked(self, request: LabwareHandoffRequest) -> None:
        await self._send("notify_picked", request.model_dump(exclude_none=True))


class RemoteProtocolRunnerDriver(_ForwardsLabwareHandoff):
    """`IProtocolRunnerDriver` whose every method forwards over the wire."""

    interfaces: ClassVar[frozenset[str]] = frozenset({"IProtocolRunner"})

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        await self._send("run_protocol", request.model_dump(exclude_none=True))


class RemotePlateWasherDriver(_ForwardsLabwareHandoff, IPlateWasherDriver):
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

    The three concrete profile classes below differ only in their
    `interfaces` ClassVar (which the orca facade + connect-time superset
    check read off `type(driver)`) and in whether `run_protocol` forwards or
    fails fast. The wire surface is uniform: the device bridge routes whatever
    command arrives.
    """

    provides_state: ClassVar[bool] = True

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


class RemoteLiquidHandlerDriver(_RemoteLiquidHandlerWireMixin, ILiquidHandlerWithProtocolDriver):
    """plr-only liquid handler (advertises `{ILiquidHandler}`).

    A driver with atomic liquid-handling commands only, and the cold-start
    default before any device bridge has advertised a richer profile. Has no protocol
    capability, so `run_protocol` fails fast at the boundary.
    """

    interfaces: ClassVar[frozenset[str]] = frozenset({"ILiquidHandler"})

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        raise self._no_protocol_runner("run_protocol")

    async def prepare_for_place(self, request: LabwareHandoffRequest) -> None:
        raise self._no_protocol_runner("prepare_for_place")

    async def notify_placed(self, request: LabwareHandoffRequest) -> None:
        raise self._no_protocol_runner("notify_placed")

    async def prepare_for_pick(self, request: LabwareHandoffRequest) -> None:
        raise self._no_protocol_runner("prepare_for_pick")

    async def notify_picked(self, request: LabwareHandoffRequest) -> None:
        raise self._no_protocol_runner("notify_picked")

    def _no_protocol_runner(self, command: str) -> RunProtocolNotSupportedError:
        return RunProtocolNotSupportedError(
            f"Device {self._name!r} advertises {sorted(self.interfaces)!r} and "
            f"has no IProtocolRunner capability; {command} is not supported."
        )


class RemoteLiquidHandlerWithProtocolDriver(
    _ForwardsLabwareHandoff, _RemoteLiquidHandlerWireMixin, ILiquidHandlerWithProtocolDriver,
):
    """plr + protocol liquid handler (advertises `{ILiquidHandler, IProtocolRunner}`).

    Supports both atomic ops and vendor protocol files.
    """

    interfaces: ClassVar[frozenset[str]] = frozenset(
        {"ILiquidHandler", "IProtocolRunner"}
    )

    async def run_protocol(self, request: RunProtocolRequest) -> None:
        await self._forward_run_protocol(request)


class RemoteProtocolOnlyLiquidHandlerDriver(
    _ForwardsLabwareHandoff, _RemoteLiquidHandlerWireMixin, ILiquidHandlerWithProtocolDriver,
):
    """protocol-only liquid handler (advertises `{IProtocolRunner}`).

    Driven only by vendor protocol files, with no orca-level atomic ops. The orca
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
