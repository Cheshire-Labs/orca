"""The requests and responses of the operator device commands. Each request names its device."""

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Literal, TypeVar

from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse,
    MixRequest as DriverMixRequest,
    PipettingPatch,
)
from cheshire_drivers.command_responses import EmptyCommandResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import Self

from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.binding import RestBinding
from orca.operations.route_tags import RouteTag
from orca.operations.wire_models import TeachpointDTO
from orca.devices.pipetting_parameters import resolve_wire_pipetting
from orca.runtime.teachpoint_wire import POSITION_ID_DESCRIPTION

T = TypeVar("T", bound=BaseModel)


class SuccessEnvelope(BaseModel, Generic[T]):
    """What a typed device command answers: a message, and the command's typed result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = True
    message: str
    result: T


def envelope_of(result: type[BaseModel]) -> type[BaseModel]:
    """The success envelope that carries `result`, built for a response model read off the command table."""
    envelope = SuccessEnvelope.__class_getitem__(result)
    if not (isinstance(envelope, type) and issubclass(envelope, BaseModel)):
        raise TypeError(f"no success envelope for {result.__name__}")
    return envelope


class DeviceRequest(BaseModel):
    """A command on one device. The driver is sent every other field that is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str = Field(..., description="The device, as the topology or its orca-client names it.")

    def driver_params(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", exclude={"device_id"}, exclude_none=True)


_REASON = Field(None, description="Why, for the audit trail.")


class BatchExecuteRequest(DeviceRequest):
    commands: list[dict[str, JsonValue]] = Field(
        ...,
        description="Commands sent in order, each with 'command', 'params', optional 'timeout', and optional "
        "'confirm' (acknowledgement for a vendor command whose catalog entry says requires_confirm). 'command' "
        "is the name GET /api/devices/{device_id}/introspection lists. An orca-side namespace ('shaker.shake') "
        "is dropped; any other prefix belongs to the command ('gripper.ungrip').",
    )
    stop_on_error: bool = Field(True, description="Stop on first error (default True)")
    reason: str | None = _REASON


class MoveToRequest(DeviceRequest):
    position_id: str = Field(..., min_length=1, max_length=100, description="Position identifier (deck slot) to move to.")


class PickPlateRequest(DeviceRequest):
    position_id: str = Field(..., min_length=1, max_length=100, description="Position identifier (deck slot) to pick from.")


class PlacePlateRequest(DeviceRequest):
    position_id: str = Field(..., min_length=1, max_length=100, description="Position identifier (deck slot) to place at.")


_POSITION_DESCRIPTION = (
    "Absolute gripper-axis position to drive to, for a caller holding a taught pose. "
    "Omit for the arm's calibrated default: the stored jaw opening past the grip "
    "position when opening, the grip position itself when closing. Neither default "
    "sweeps the jaws to a physical stop."
)


class OpenGripperRequest(DeviceRequest):
    jaw_opening: float | None = Field(
        default=None,
        description="How far past the calibrated grip position to open, in gripper-axis units. Omit to use "
        "this arm's stored jaw opening, which is the same number a pick opens by.",
    )
    position: float | None = Field(default=None, description=_POSITION_DESCRIPTION)

    @model_validator(mode="after")
    def _one_way_of_saying_how_far(self) -> Self:
        if self.position is not None and self.jaw_opening is not None:
            raise ValueError(
                "open_gripper takes jaw_opening (a step past the grip position) or "
                "position (an absolute axis target), not both."
            )
        return self


class SaveCurrentPositionRequest(DeviceRequest):
    position_id: str = Field(..., description=POSITION_ID_DESCRIPTION)
    coord_type: Literal["cartesian", "joint"] = Field(default="cartesian")
    access_config_name: str | None = Field(default=None)
    gateway: str | None = Field(default=None)
    orientation: Literal["left", "right"] | None = Field(default=None)
    reason: str | None = Field(default=None)

    @model_validator(mode="after")
    def _orientation_matches_coord_type(self) -> Self:
        if self.coord_type == "cartesian" and self.orientation is None:
            raise ValueError("Cartesian teachpoints require orientation ('left' or 'right')")
        return self


class ConfigureDeckRequest(DeviceRequest):
    layout_name: str = Field(
        ..., min_length=1, max_length=128,
        description="The stored deck layout to push. Store it first with PUT /api/deck-layouts/{device_id}/{name}.",
    )
    reason: str | None = Field(None, description="Why, for the audit trail.")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_LIQUID_CLASS_FIELD = Field(
    None,
    description="How this liquid behaves, e.g. {\"flow_rate\": 20.0} for a viscous reagent. Reusable across calls.",
)
_TECHNIQUE_FIELD = Field(
    None,
    description="What this one step needs, e.g. {\"height\": 3.0} for the tip height above the well floor in mm. "
    "Wins over liquid_class field by field.",
)


class _LiquidTarget(_Strict):
    labware: str = Field(..., min_length=1, max_length=100, description="Name of the labware")
    positions: list[str] | None = Field(
        None, max_length=96, description="Well positions per channel; omit for a single-container (trough) slice",
    )
    volumes: list[float] = Field(..., min_length=1, max_length=96, description="Volume in uL for each channel (must be > 0)")

    @model_validator(mode="after")
    def _positions_match_volumes(self) -> Self:
        if self.positions is not None and len(self.positions) != len(self.volumes):
            raise ValueError(
                f"{type(self).__name__} '{self.labware}': positions ({len(self.positions)}) "
                f"and volumes ({len(self.volumes)}) must be the same length"
            )
        return self


class AspirateTarget(_LiquidTarget):
    """One labware's slice of an 8-channel aspirate. Each entry contributes ``len(volumes)`` channels."""


class DispenseTarget(_LiquidTarget):
    """One labware's slice of an 8-channel dispense. See ``AspirateTarget``."""


class AspirateRequest(DeviceRequest):
    aspirations: list[AspirateTarget] = Field(
        ..., min_length=1, description="Per-labware slices that make up the 8-channel aspirate",
    )
    flow_rate: float | None = Field(None, gt=0, description="Aspiration speed in uL/s")
    liquid_class: PipettingPatch | None = _LIQUID_CLASS_FIELD
    technique: PipettingPatch | None = _TECHNIQUE_FIELD
    use_channels: list[int] | None = Field(None, description="Which channels to use (0-7)")

    def driver_params(self) -> dict[str, JsonValue]:
        return _pipetting_params(self)


class DispenseRequest(DeviceRequest):
    dispenses: list[DispenseTarget] = Field(
        ..., min_length=1, description="Per-labware slices that make up the 8-channel dispense",
    )
    flow_rate: float | None = Field(None, gt=0, description="Dispense speed in uL/s")
    liquid_class: PipettingPatch | None = _LIQUID_CLASS_FIELD
    technique: PipettingPatch | None = _TECHNIQUE_FIELD
    use_channels: list[int] | None = Field(None, description="Which channels to use (0-7)")

    def driver_params(self) -> dict[str, JsonValue]:
        return _pipetting_params(self)


def _pipetting_params(req: AspirateRequest | DispenseRequest) -> dict[str, JsonValue]:
    params = req.model_dump(mode="json", exclude={"device_id", "liquid_class", "technique"}, exclude_none=True)
    params["parameters"] = resolve_wire_pipetting(req.liquid_class, req.technique).model_dump(mode="json")
    return params


class MixRequest(DeviceRequest):
    """The operator states the pipetting layers; the resolver a workflow uses folds them."""

    labware: str = Field(..., min_length=1, description="Name of the labware to mix in")
    positions: list[str] | None = Field(
        None,
        description="Well ids, one per channel, for an itemized labware; omit for a single-container trough "
        "and give use_channels instead",
    )
    volume: float = Field(..., gt=0, description="Volume per cycle in uL")
    repetitions: int = Field(..., gt=0, description="Number of aspirate/dispense cycles")
    liquid_class: PipettingPatch | None = _LIQUID_CLASS_FIELD
    technique: PipettingPatch | None = _TECHNIQUE_FIELD
    use_channels: list[int] | None = Field(None, description="Which channels to use (0-7)")

    def driver_params(self) -> dict[str, JsonValue]:
        # Built as the driver's own model so its cross-field rules refuse here, not at the lab.
        try:
            mix = DriverMixRequest.model_validate({
                "labware": self.labware,
                "positions": self.positions,
                "volume": self.volume,
                "repetitions": self.repetitions,
                "parameters": resolve_wire_pipetting(self.liquid_class, self.technique),
                "use_channels": self.use_channels,
            })
        except PydanticValidationError as exc:
            errors: list[JsonValue] = [
                {"loc": [str(part) for part in error["loc"]], "msg": error["msg"], "type": error["type"]}
                for error in exc.errors(include_url=False, include_context=False)
            ]
            raise OperationError.of(
                ErrorCode.VALIDATION_ERROR, f"Invalid {DriverMixRequest.__name__}", validation_errors=errors,
            ) from exc
        return mix.model_dump(mode="json", exclude_none=True)


PipettingRequest = AspirateRequest | DispenseRequest | MixRequest


class BatchCommandResultDTO(BaseModel):
    """One command's outcome in a batch, on success and under `detail.extras.results` on failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    success: bool
    duration_ms: float
    result: JsonValue = None
    error: str | None = None
    error_type: str | None = None


class BatchFailureExtras(BaseModel):
    """The extras a failed batch's `batch_error` carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failed_at_index: int | None
    results: list[BatchCommandResultDTO]
    total_duration_ms: float


class BatchExecuteResponse(BaseModel):
    """A batch that ran to the end. A failed one raises `batch_error` instead."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    results: list[BatchCommandResultDTO]
    total_duration_ms: float


class TransporterCommandResponse(SuccessEnvelope[EmptyCommandResponse]):
    pass


class ConfigureDeckResponse(SuccessEnvelope[LabwareStateResponse]):
    pass


class LiquidHandlerPipettingResponse(SuccessEnvelope[LabwareStateResponse]):
    pass


class DeviceKind(Enum):
    """The path a device command is served under, and the interface a device needs for it."""

    DEVICE = ("devices", None, None, "device", RouteTag.DEVICES)
    TRANSPORTER = ("transporters", "ITransporter", ErrorCode.TRANSPORTER_NOT_FOUND, "transporter", RouteTag.TRANSPORTERS)
    LIQUID_HANDLER = (
        "liquid-handlers", "ILiquidHandler", ErrorCode.LIQUID_HANDLER_NOT_FOUND, "liquid handler",
        RouteTag.LIQUID_HANDLERS,
    )

    def __init__(
        self, path: str, interface: str | None, not_found: ErrorCode | None, label: str, tag: RouteTag,
    ) -> None:
        self.path = path
        self.interface = interface
        self.not_found = not_found
        self.label = label
        self.tag = tag


@dataclass(frozen=True)
class OperatorRoute:
    """Where an operator device command is served, and what it takes and answers. One copy, read by the Operation and the CLI."""

    request: type[BaseModel]
    response: type[BaseModel]
    binding: RestBinding


def _on(kind: DeviceKind, route: str) -> RestBinding:
    return RestBinding(method="POST", path=f"/{kind.path}/{{device_id}}/{route}", tag=kind.tag, drives_device=True)


BATCH_EXECUTE = OperatorRoute(BatchExecuteRequest, BatchExecuteResponse, _on(DeviceKind.DEVICE, "commands"))
MOVE_TO = OperatorRoute(MoveToRequest, TransporterCommandResponse, _on(DeviceKind.TRANSPORTER, "move-to"))
PICK_PLATE = OperatorRoute(PickPlateRequest, TransporterCommandResponse, _on(DeviceKind.TRANSPORTER, "pick-plate"))
PLACE_PLATE = OperatorRoute(PlacePlateRequest, TransporterCommandResponse, _on(DeviceKind.TRANSPORTER, "place-plate"))
OPEN_GRIPPER = OperatorRoute(OpenGripperRequest, TransporterCommandResponse, _on(DeviceKind.TRANSPORTER, "open-gripper"))
CONFIGURE_DECK = OperatorRoute(
    ConfigureDeckRequest, ConfigureDeckResponse, _on(DeviceKind.LIQUID_HANDLER, "configure-deck"),
)
ASPIRATE = OperatorRoute(AspirateRequest, LiquidHandlerPipettingResponse, _on(DeviceKind.LIQUID_HANDLER, "aspirate"))
DISPENSE = OperatorRoute(DispenseRequest, LiquidHandlerPipettingResponse, _on(DeviceKind.LIQUID_HANDLER, "dispense"))
MIX = OperatorRoute(MixRequest, LiquidHandlerPipettingResponse, _on(DeviceKind.LIQUID_HANDLER, "mix"))
SAVE_CURRENT_POSITION = OperatorRoute(
    SaveCurrentPositionRequest, TeachpointDTO,
    RestBinding(
        method="POST", path="/teachpoints/{device_id}/capture", tag=RouteTag.TEACHPOINTS, status_code=201,
        drives_device=True,
    ),
)
