"""Operator device commands that resolve something before they send: served the same way on every server.

Each one runs through `IDeviceCommandRunner`, so it takes the device's external control and lock and is saved
before it answers. An Operation that replaces a plain command names it in `claims`.
"""

import logging
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import ClassVar, Protocol

from typing_extensions import Self

from cheshire_drivers.command_table import COMMANDS
from cheshire_drivers.liquid_handler_models import LabwareStateResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from orca.gateway.controller.command_pipeline import PartialChannelFailureError
from orca.operations.error_mapping import pipeline_error, teachpoint_not_found
from orca.resource_models.command_effects import reach_params
from orca.resource_models.device_error import TeachpointNotFoundError, TipsNotPresentError
from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    CommandNotSavedError,
    CommandTimeoutError,
    ConfirmationRequiredError,
    DeviceError,
    DeviceLockedError,
    DeviceOfflineError,
    InvalidCommandError,
)
from orca.gateway.device_command_runner import IDeviceCommandRunner, NamedCommandResult
from orca.gateway.mode_resolution import refuse_unbuilt
from orca.gateway.operator_commands import UnreadableDriverAnswerError
from orca.gateway.registry import DeviceSnapshot
from orca.operations.binding import BoundOperation, RestBinding
from orca.operations.device import LIFECYCLE_OPERATIONS
from orca.operations.device_commands import (
    ClaimedCommandByNameError,
    LifecycleCommandByNameError,
    device_error,
    device_not_found,
)
from orca.operations.labware import slot_occupied
from orca.resource_models.device_error import SlotOccupiedError
from orca.operations.operator_device_models import (
    ASPIRATE,
    BATCH_EXECUTE,
    CONFIGURE_DECK,
    DISPENSE,
    MIX,
    MOVE_TO,
    OPEN_GRIPPER,
    PICK_PLATE,
    PLACE_PLATE,
    SAVE_CURRENT_POSITION,
    DeviceKind,
    OperatorRoute,
    BatchCommandResultDTO,
    BatchExecuteRequest,
    BatchExecuteResponse,
    BatchFailureExtras,
    ConfigureDeckRequest,
    ConfigureDeckResponse,
    LiquidHandlerPipettingResponse,
    MoveToRequest,
    OpenGripperRequest,
    PickPlateRequest,
    PipettingRequest,
    PlacePlateRequest,
    SaveCurrentPositionRequest,
    TransporterCommandResponse,
)
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.operations.wire_models import TeachpointDTO
from orca.operations.teachpoints import build_from_wire
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.facades.device_modes import CAPTURE_IN_SIM_REFUSED
from orca.resource_models.move_parameters import move_defaults_for_device
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.setup_files import SetupEntryExistsError
from orca.runtime.teachpoint_resolver import TeachpointResolver
from orca.runtime.teachpoint_wire import validate_coords

logger = logging.getLogger(__name__)



# What each claim's first part means: the interface a claimed command is looked up on.
CLAIM_KINDS: dict[str, DeviceKind] = {
    "device": DeviceKind.DEVICE,
    "transporter": DeviceKind.TRANSPORTER,
    "liquid_handler": DeviceKind.LIQUID_HANDLER,
}


@contextmanager
def answering_for(device_id: str) -> Iterator[None]:
    """A device failure answers with the code every surface uses for it, naming the device."""
    try:
        yield
    except DeviceError as exc:
        raise device_error(exc, device_id) from exc
    except (TipsNotPresentError, PartialChannelFailureError, TeachpointNotFoundError) as exc:
        known = pipeline_error(exc)
        if known is None:
            raise
        raise known from exc
    except SlotOccupiedError as exc:
        raise slot_occupied(exc) from exc
    except UnreadableDriverAnswerError as exc:
        raise OperationError.of(
            ErrorCode.DRIVER_FAILURE, f"Driver return shape did not match the wire contract: {exc}", device_id=device_id,
        ) from exc


async def require_kind(devices: IDeviceCommandRunner, device_id: str, kind: DeviceKind) -> DeviceSnapshot:
    """The device, refused with the kind's own not-found code when it does not advertise the kind's interface."""
    with answering_for(device_id):
        try:
            device = await devices.device(device_id)
        except RuntimeError as exc:
            # The connection roster raises RuntimeError when the caller may not see the device.
            raise OperationError.of(
                ErrorCode.AUTH_ERROR, str(exc),
            ) from exc
    if kind.interface is not None and kind.interface not in device.interfaces:
        assert kind.not_found is not None
        raise OperationError.of(
            kind.not_found,
            f"resource {device_id!r} is not a {kind.label} (advertises {sorted(device.interfaces)!r})",
            device_id=device_id,
        )
    return device


def check_labware_state(device_id: str, command: str, result: BaseModel) -> None:
    """A labware command whose driver reports failure is an error, not a 200."""
    if isinstance(result, LabwareStateResponse) and not result.success:
        raise OperationError.of(
            ErrorCode.DRIVER_FAILURE, f"Driver reported failure for {command}", device_id=device_id,
            labware_state={name: well.model_dump(mode="json") for name, well in result.labware_state.items()},
        )


def claims_name_commands(op_classes: tuple[type, ...]) -> None:
    """Raise if a claim names no command, or two Operations claim the same one."""
    seen: dict[tuple[str, str], str] = {}
    for op_class in op_classes:
        for claim in getattr(op_class, "claims", ()):
            kind_name, command = claim
            kind = CLAIM_KINDS.get(kind_name)
            if kind is None or kind.interface is None or command not in COMMANDS.commands_of(kind.interface):
                raise ValueError(f"{op_class.__name__} claims {claim!r}, which names no command")
            if claim in seen:
                raise ValueError(f"{op_class.__name__} and {seen[claim]} both claim {claim!r}")
            seen[claim] = op_class.__name__


async def _resolved(runtime: ISystemRuntime, device_id: str, position_id: str) -> dict[str, JsonValue]:
    try:
        return await TeachpointResolver(runtime.teachpoints).resolve(device_id, position_id)
    except KeyError as exc:
        raise device_not_found(device_id) from exc
    except TeachpointNotFoundError as exc:
        raise teachpoint_not_found(exc) from exc


_ARM = "ITransporter"
_LH = "ILiquidHandler"


async def _send(
    devices: IDeviceCommandRunner, device_id: str, interface: str, name: str, params: Mapping[str, JsonValue],
) -> BaseModel:
    """Run the command `interface` declares under `name`, with its request built from `params`."""
    command = COMMANDS.commands_of(interface)[name]
    return await devices.run(device_id, command, command.validate(params))


def _done(message: str) -> TransporterCommandResponse:
    return TransporterCommandResponse.model_validate({"message": message, "result": {}})


class TakesTheRunner(BoundOperation, Protocol):
    """An Operation built from the device command runner alone."""

    def __init__(self, *, devices: IDeviceCommandRunner) -> None: ...


class ClaimsCommands(BoundOperation, Protocol):
    """An Operation that replaces plain commands, named in `claims` as (kind, command)."""

    claims: ClassVar[tuple[tuple[str, str], ...]]


class ClaimsAndTakesTheRunner(TakesTheRunner, Protocol):
    """A runner-only Operation that replaces plain commands."""

    claims: ClassVar[tuple[tuple[str, str], ...]]


class TakesTheRuntimeAndTheRunner(BoundOperation, Protocol):
    """An Operation built from the runtime and the device command runner."""

    def __init__(self, *, runtime: ISystemRuntime, devices: IDeviceCommandRunner) -> None: ...


class BatchCommand(BaseModel):
    """One command sent by name, checked before any command runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    params: dict[str, JsonValue] = Field(default_factory=dict)
    timeout: float | None = None
    confirm: bool = False


_BATCH_COMMAND_KEYS = frozenset(BatchCommand.model_fields)


def _invalid_batch(message: str) -> OperationError:
    return OperationError.validation_error(message)


def batch_commands(req: BatchExecuteRequest) -> list[BatchCommand]:
    """The batch's commands, refused as a whole when any one is malformed."""
    commands: list[BatchCommand] = []
    for i, cmd in enumerate(req.commands):
        name = cmd.get("command")
        if name is None:
            raise _invalid_batch(f"Command at index {i} missing 'command' field")
        unknown = sorted(cmd.keys() - _BATCH_COMMAND_KEYS)
        if unknown:
            raise _invalid_batch(f"Command at index {i} has unknown fields: {', '.join(unknown)}")
        confirm = cmd.get("confirm", False)
        if not isinstance(confirm, bool):
            # bool("false") is True; coercion would open the safety gate.
            raise _invalid_batch(f"Command at index {i}: 'confirm' must be a boolean, got {type(confirm).__name__}")
        params = cmd.get("params", {})
        timeout = cmd.get("timeout")
        if not isinstance(name, str) or not isinstance(params, dict) or not (
            timeout is None or (isinstance(timeout, (int, float)) and not isinstance(timeout, bool))
        ):
            raise _invalid_batch(
                f"Command at index {i}: 'command' is a string, 'params' an object and 'timeout' a number",
            )
        commands.append(BatchCommand(
            command=name, params=params, timeout=None if timeout is None else float(timeout), confirm=confirm,
        ))
    return commands


def reads_a_property(device: DeviceSnapshot, name: str) -> bool:
    """Whether `name` is a driver property on this device: a reading the device registry holds."""
    advertised = device.methods.get(name)
    if advertised is not None:
        return advertised.kind == "property"
    command = COMMANDS.find(device.interfaces, name)
    return command is not None and command.is_property


class NamedCommands:
    """Commands sent to one device by name.

    An operator's hold keeps runs off the device, not the operator: these go through while it is held.
    """

    def __init__(self, runtime: ISystemRuntime, devices: IDeviceCommandRunner, device_id: str) -> None:
        self._runtime = runtime
        self._devices = devices
        self._device_id = device_id

    @asynccontextmanager
    async def held(self) -> AsyncIterator[Self]:
        """Hold the device across every command sent inside, so the engine cannot take it between two of them."""
        async with self._devices.hold(self._device_id) as held:
            yield type(self)(self._runtime, held, self._device_id)

    def name_of(self, command: BatchCommand, device: DeviceSnapshot) -> str:
        """The name the device is sent, refused when an Operation of its own sends it: a lifecycle command or a claimed one."""
        try:
            name = self._runtime.devices.command_name(self._device_id, command.command)
        except KeyError as exc:
            raise device_not_found(self._device_id) from exc
        if name in COMMANDS.lifecycle_names():
            raise LifecycleCommandByNameError(name, self._device_id, LIFECYCLE_OPERATIONS[name])
        claimed = claimed_by(name, device)
        if claimed is not None:
            raise ClaimedCommandByNameError(name, self._device_id, claimed.binding)
        return name

    async def run(self, command: BatchCommand, reason: str | None) -> NamedCommandResult:
        device = await self._devices.device(self._device_id)
        name = self.name_of(command, device)
        if reads_a_property(device, name):
            raise InvalidCommandError(
                f"{name!r} on {self._device_id!r} is a reading, not a command. "
                f"Read it from GET /api/devices/{self._device_id}."
            )
        return await self._devices.run_named(
            self._device_id, name, command.params, timeout_seconds=command.timeout, confirm=command.confirm,
            reason=reason,
        )


def named_commands(runtime: ISystemRuntime | None, devices: IDeviceCommandRunner, device_id: str) -> NamedCommands:
    """Commands by name to a device the runner knows; refused while no runtime is built, as the runner refuses."""
    if runtime is None:
        refuse_unbuilt(device_id)
    return NamedCommands(runtime, devices, device_id)


def _error_type(exc: DeviceError) -> str:
    if isinstance(exc, CommandExecutionError):
        return exc.error_type
    if isinstance(exc, CommandTimeoutError):
        # A timeout whose outcome an incident now tracks is still a timeout to a batch reader.
        return CommandTimeoutError.__name__
    return type(exc).__name__


async def _run_one(named: NamedCommands, command: BatchCommand, reason: str | None) -> BatchCommandResultDTO:
    """One batch command's outcome. A failure is reported, not raised, so the batch can say how far it got."""
    started = time.perf_counter()
    try:
        ran = await named.run(command, reason)
    except DeviceLockedError as exc:
        return BatchCommandResultDTO(
            command=command.command, success=False, duration_ms=0.0, error=str(exc), error_type="DeviceLockedError",
        )
    except (
        CommandNotSavedError, ConfirmationRequiredError, DeviceOfflineError, CommandTimeoutError, CommandExecutionError,
    ) as exc:
        return BatchCommandResultDTO(
            command=command.command, success=False, duration_ms=(time.perf_counter() - started) * 1000,
            error=str(exc), error_type=_error_type(exc),
        )
    except (TipsNotPresentError, PartialChannelFailureError) as exc:
        return BatchCommandResultDTO(
            command=command.command, success=False, duration_ms=(time.perf_counter() - started) * 1000,
            error=str(exc), error_type=type(exc).__name__,
        )
    return BatchCommandResultDTO(command=command.command, success=True, duration_ms=ran.duration_ms, result=ran.value)


async def run_batch(
    named: NamedCommands, commands: list[BatchCommand], *, stop_on_error: bool, reason: str | None,
) -> tuple[list[BatchCommandResultDTO], int | None, float]:
    """Run the commands in order under one hold."""
    started = time.perf_counter()
    results: list[BatchCommandResultDTO] = []
    failed_at: int | None = None
    async with named.held() as held:
        for index, command in enumerate(commands):
            result = await _run_one(held, command, reason)
            results.append(result)
            if not result.success:
                failed_at = index
                # Every later command would act on a record missing this one.
                if stop_on_error or result.error_type == "CommandNotSavedError":
                    break
    return results, failed_at, (time.perf_counter() - started) * 1000


class DeviceBatchExecuteOperation:
    """Run commands in order on one device under one hold, stopping at the first failure unless told not to."""

    Request: ClassVar[type[BaseModel]] = BATCH_EXECUTE.request
    Response: ClassVar[type[BaseModel]] = BATCH_EXECUTE.response
    binding: ClassVar[RestBinding] = BATCH_EXECUTE.binding

    def __init__(self, *, runtime: ISystemRuntime | None, devices: IDeviceCommandRunner) -> None:
        self._runtime = runtime
        self._devices = devices

    async def run(self, req: BatchExecuteRequest) -> BatchExecuteResponse:
        device = await require_kind(self._devices, req.device_id, DeviceKind.DEVICE)
        commands = batch_commands(req)
        with answering_for(req.device_id):
            named = named_commands(self._runtime, self._devices, req.device_id)
            # Refused before any command runs, so a batch never stops half-done on a name it could have refused.
            for command in commands:
                named.name_of(command, device)
            results, failed_at, total_ms = await run_batch(
                named, commands,
                stop_on_error=req.stop_on_error, reason=req.reason,
            )
        if failed_at is None:
            return BatchExecuteResponse(success=True, results=results, total_duration_ms=total_ms)
        # A failed command makes the batch an error; what ran before it rides in the extras.
        extras = BatchFailureExtras(failed_at_index=failed_at, results=results, total_duration_ms=total_ms)
        raise OperationError.of(
            ErrorCode.BATCH_ERROR, f"batch failed at command {failed_at}", **extras.model_dump(mode="json"),
        )


class TransporterMoveToOperation:
    """Move the transporter to a teachpoint."""

    Request: ClassVar[type[BaseModel]] = MOVE_TO.request
    Response: ClassVar[type[BaseModel]] = MOVE_TO.response
    binding: ClassVar[RestBinding] = MOVE_TO.binding
    claims: ClassVar[tuple[tuple[str, str], ...]] = ()

    def __init__(self, *, devices: IDeviceCommandRunner, runtime: ISystemRuntime) -> None:
        self._devices = devices
        self._runtime = runtime

    async def run(self, req: MoveToRequest) -> TransporterCommandResponse:
        await require_kind(self._devices, req.device_id, DeviceKind.TRANSPORTER)
        teachpoint = await _resolved(self._runtime, req.device_id, req.position_id)
        with answering_for(req.device_id):
            await _send(self._devices, req.device_id, _ARM, "move_to_coords", {"teachpoint": teachpoint})
        return _done(f"moved {req.device_id} to {req.position_id}")


@asynccontextmanager
async def _holding(devices: IDeviceCommandRunner, device_id: str) -> AsyncIterator[IDeviceCommandRunner]:
    with answering_for(device_id):
        async with devices.hold(device_id) as held:
            yield held


async def _reach(devices: IDeviceCommandRunner, device_id: str, command: str, position_id: str) -> None:
    """A pick or place at a site, held across the move. The arm resolves its own taught position, waypoints and grip."""
    # Not a driver request: the runner hands a pick or place to the transporter, which builds the request itself.
    async with _holding(devices, device_id) as held:
        await held.run_named(device_id, command, reach_params(position_id))


class TransporterPickPlateOperation:
    """Pick up a plate from a site, as a run's move does: through its gateway waypoints, holding the arm throughout."""

    Request: ClassVar[type[BaseModel]] = PICK_PLATE.request
    Response: ClassVar[type[BaseModel]] = PICK_PLATE.response
    binding: ClassVar[RestBinding] = PICK_PLATE.binding
    claims: ClassVar[tuple[tuple[str, str], ...]] = (("transporter", "pick_at_coords"),)

    def __init__(self, *, devices: IDeviceCommandRunner) -> None:
        self._devices = devices

    async def run(self, req: PickPlateRequest) -> TransporterCommandResponse:
        await require_kind(self._devices, req.device_id, DeviceKind.TRANSPORTER)
        await _reach(self._devices, req.device_id, "pick_at_coords", req.position_id)
        return _done(f"picked plate from {req.position_id} with {req.device_id}")


class TransporterPlacePlateOperation:
    """Place a plate at a site, as a run's move does: through its gateway waypoints, holding the arm throughout."""

    Request: ClassVar[type[BaseModel]] = PLACE_PLATE.request
    Response: ClassVar[type[BaseModel]] = PLACE_PLATE.response
    binding: ClassVar[RestBinding] = PLACE_PLATE.binding
    claims: ClassVar[tuple[tuple[str, str], ...]] = (("transporter", "place_at_coords"),)

    def __init__(self, *, devices: IDeviceCommandRunner) -> None:
        self._devices = devices

    async def run(self, req: PlacePlateRequest) -> TransporterCommandResponse:
        await require_kind(self._devices, req.device_id, DeviceKind.TRANSPORTER)
        await _reach(self._devices, req.device_id, "place_at_coords", req.position_id)
        return _done(f"placed plate at {req.position_id} with {req.device_id}")


class TransporterOpenGripperOperation:
    """Open the jaws past the grip position, by this arm's stored opening or one named here."""

    Request: ClassVar[type[BaseModel]] = OPEN_GRIPPER.request
    Response: ClassVar[type[BaseModel]] = OPEN_GRIPPER.response
    binding: ClassVar[RestBinding] = OPEN_GRIPPER.binding
    claims: ClassVar[tuple[tuple[str, str], ...]] = (("transporter", "open_gripper"),)

    def __init__(self, *, devices: IDeviceCommandRunner, runtime: ISystemRuntime | None) -> None:
        self._devices = devices
        self._runtime = runtime

    async def run(self, req: OpenGripperRequest) -> TransporterCommandResponse:
        await require_kind(self._devices, req.device_id, DeviceKind.TRANSPORTER)
        params = req.driver_params()
        if req.position is None and req.jaw_opening is None:
            # The same stored number a pick opens by, so the button and the pick open alike.
            defaults = await move_defaults_for_device(
                self._runtime.system if self._runtime is not None else None, req.device_id,
            )
            params["jaw_opening"] = defaults.jaw_opening
        with answering_for(req.device_id):
            await _send(self._devices, req.device_id, _ARM, "open_gripper", params)
        return _done(f"opened gripper on {req.device_id}")


class LiquidHandlerConfigureDeckOperation:
    """Push a stored deck layout to the driver. `PUT /api/deck-layouts/{device_id}/{name}` stores it; `initialize` is a separate call."""

    Request: ClassVar[type[BaseModel]] = CONFIGURE_DECK.request
    Response: ClassVar[type[BaseModel]] = CONFIGURE_DECK.response
    binding: ClassVar[RestBinding] = CONFIGURE_DECK.binding
    claims: ClassVar[tuple[tuple[str, str], ...]] = (("liquid_handler", "configure_deck"),)

    def __init__(self, *, devices: IDeviceCommandRunner, runtime: ISystemRuntime) -> None:
        self._devices = devices
        self._runtime = runtime

    async def run(self, req: ConfigureDeckRequest) -> ConfigureDeckResponse:
        await require_kind(self._devices, req.device_id, DeviceKind.LIQUID_HANDLER)
        try:
            config = await self._runtime.deck_layouts.get(req.device_id, req.layout_name)
        except KeyError as exc:
            raise OperationError.of(ErrorCode.LIQUID_HANDLER_NOT_FOUND, message_of(exc), device_id=req.device_id) from exc
        if config is None:
            raise OperationError.of(
                ErrorCode.DECK_LAYOUT_NOT_FOUND,
                f"Deck layout '{req.layout_name}' not found for device {req.device_id}. "
                f"Store it first with PUT /api/deck-layouts/{req.device_id}/{req.layout_name}.",
                device_id=req.device_id,
            )
        logger.info("configure_deck device=%s layout=%s", req.device_id, req.layout_name)
        with answering_for(req.device_id):
            result = await _send(
                self._devices, req.device_id, _LH, "configure_deck", config.model_dump(mode="json", exclude_none=True),
            )
        check_labware_state(req.device_id, "configure_deck", result)
        return ConfigureDeckResponse.model_validate({
            "message": f"configured deck {req.layout_name!r} on liquid handler {req.device_id}",
            "result": result,
        })


def _pipetting(route: str, served: OperatorRoute) -> type[ClaimsAndTakesTheRunner]:
    """An aspirate, dispense or mix: the operator states the pipetting layers, and the resolver a workflow uses folds them."""

    class PipettingOperation:
        Request: ClassVar[type[BaseModel]] = served.request
        Response: ClassVar[type[BaseModel]] = served.response
        binding: ClassVar[RestBinding] = served.binding
        claims: ClassVar[tuple[tuple[str, str], ...]] = (("liquid_handler", route),)

        def __init__(self, *, devices: IDeviceCommandRunner) -> None:
            self._devices = devices

        async def run(self, req: PipettingRequest) -> LiquidHandlerPipettingResponse:
            await require_kind(self._devices, req.device_id, DeviceKind.LIQUID_HANDLER)
            params = req.driver_params()
            with answering_for(req.device_id):
                result = await _send(self._devices, req.device_id, _LH, route, params)
            check_labware_state(req.device_id, route, result)
            return LiquidHandlerPipettingResponse.model_validate(
                {"message": f"{route} on {req.device_id}", "result": result},
            )

    PipettingOperation.__name__ = PipettingOperation.__qualname__ = f"LiquidHandler{route.capitalize()}Operation"
    PipettingOperation.__doc__ = f"Send `{route}` to a liquid handler, with the pipetting layers the operator states."
    return PipettingOperation


LiquidHandlerAspirateOperation = _pipetting("aspirate", ASPIRATE)
LiquidHandlerDispenseOperation = _pipetting("dispense", DISPENSE)
LiquidHandlerMixOperation = _pipetting("mix", MIX)


class SaveCurrentPositionOperation:
    """Save where the arm stands as a teachpoint: check the name, read the position, register it."""

    Request: ClassVar[type[BaseModel]] = SAVE_CURRENT_POSITION.request
    Response: ClassVar[type[BaseModel]] = SAVE_CURRENT_POSITION.response
    binding: ClassVar[RestBinding] = SAVE_CURRENT_POSITION.binding

    def __init__(
        self, *, devices: IDeviceCommandRunner, runtime: ISystemRuntime | None, registries: IDeploymentRegistries,
    ) -> None:
        self._devices = devices
        self._runtime = runtime
        self._registries = registries

    async def run(self, req: SaveCurrentPositionRequest) -> TeachpointDTO:
        device_id = req.device_id
        await require_kind(self._devices, device_id, DeviceKind.DEVICE)
        runtime = self._runtime
        if runtime is None:
            raise OperationError.runtime_not_ready("the runtime is not built")
        if not await runtime.device_modes.may_capture(device_id, under=runtime.operator_base_mode()):
            raise OperationError.of(
                ErrorCode.CAPTURE_IN_SIM_REFUSED, CAPTURE_IN_SIM_REFUSED, device_id=device_id,
            )
        command = "get_cartesian_position" if req.coord_type == "cartesian" else "get_joint_position"
        with answering_for(device_id):
            position = (await _send(self._devices, device_id, _ARM, command, {})).model_dump(mode="json")
        if not position:
            raise OperationError.of(
                ErrorCode.DRIVER_FAILURE, "Failed to get current position from robot", device_id=device_id,
            )
        # A reading missing an axis would store a wrong teachpoint, because missing axes default to 0.0.
        try:
            validate_coords(req.coord_type, position)
        except ValueError as exc:
            raise OperationError.of(
                ErrorCode.POSITION_ERROR, f"device returned an invalid {req.coord_type} position: {exc}", device_id=device_id,
            ) from exc
        try:
            teachpoint = await build_from_wire(
                self._registries.access_configs,
                position_id=req.position_id,
                coord_type=req.coord_type,
                coords=position,
                access_config_name=req.access_config_name,
                gateway=req.gateway,
                orientation=req.orientation,
            )
        except (KeyError, ValueError) as exc:
            raise _validation_error(message_of(exc), device_id) from exc
        try:
            await runtime.teachpoints.add(device_id, teachpoint, reason=req.reason, confirm=True)
        except KeyError as exc:
            raise device_not_found(device_id) from exc
        except SetupEntryExistsError as exc:
            raise OperationError.of(
                ErrorCode.ALREADY_EXISTS, str(exc), device_id=device_id,
            ) from exc
        except ValueError as exc:
            raise _validation_error(str(exc), device_id) from exc
        logger.info("saved current position as teachpoint '%s' for device %s (%s)", req.position_id, device_id, req.coord_type)
        return TeachpointDTO.from_value(device_id, teachpoint)


def _validation_error(message: str, device_id: str) -> OperationError:
    return OperationError.of(
        ErrorCode.VALIDATION_ERROR, message, device_id=device_id,
    )


DEVICE_COMMAND_RUNNER_OPERATIONS: tuple[type[TakesTheRunner], ...] = (
    TransporterPickPlateOperation,
    TransporterPlacePlateOperation,
    LiquidHandlerAspirateOperation,
    LiquidHandlerDispenseOperation,
    LiquidHandlerMixOperation,
)
"""Operations built from the runner alone."""

COMMANDS_BY_NAME = (DeviceBatchExecuteOperation,)
"""Commands sent by name."""

OWN_LOGIC_OPERATIONS: tuple[type[ClaimsCommands], ...] = (
    LiquidHandlerConfigureDeckOperation,
    LiquidHandlerAspirateOperation,
    LiquidHandlerDispenseOperation,
    LiquidHandlerMixOperation,
    TransporterMoveToOperation,
    TransporterPickPlateOperation,
    TransporterPlacePlateOperation,
    TransporterOpenGripperOperation,
)
"""The operator commands that resolve something before they send, one Operation each."""


def claimed_by(name: str, device: DeviceSnapshot) -> type[BoundOperation] | None:
    """The Operation that claims the command `name` on this device, if one does."""
    for op_class in OWN_LOGIC_OPERATIONS:
        for kind_name, command in op_class.claims:
            interface = CLAIM_KINDS[kind_name].interface
            if command == name and interface is not None and interface in device.interfaces:
                return op_class
    return None
