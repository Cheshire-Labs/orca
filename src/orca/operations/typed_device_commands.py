"""The typed device routes that send one driver command: one Operation each, built from this table.

The table holds names only: where each command is served and which interface declares it. Its request
model, response model and time budget come from cheshire-drivers' command table.
"""

from dataclasses import dataclass
from typing import ClassVar

from cheshire_drivers.command_table import COMMANDS, DeviceCommand
from pydantic import BaseModel, ConfigDict, Field

from orca.gateway.device_command_runner import IDeviceCommandRunner
from orca.operations.binding import HttpMethod, RestBinding
from orca.operations.operator_device import TakesTheRunner, answering_for, check_labware_state, require_kind
from orca.operations.operator_device_models import DeviceKind, envelope_of

_CLASS_PREFIX = {
    DeviceKind.DEVICE: "Device",
    DeviceKind.TRANSPORTER: "Transporter",
    DeviceKind.LIQUID_HANDLER: "LiquidHandler",
}
_KIND_OF_PATH = {kind.path: kind for kind in DeviceKind}


@dataclass(frozen=True)
class TypedRoute:
    """`/<kind>/{device_id}/<segment>` serves `command` as `interface` declares it."""

    path: str
    interface: str
    command: str


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_"))


def _method_of(command: DeviceCommand) -> HttpMethod:
    # A read moves nothing, so it is a GET; everything else is a POST.
    return "GET" if command.name.startswith("get_") else "POST"


def _request_on_a_device(name: str, command: DeviceCommand) -> type[BaseModel]:
    """The driver's request model, with the device it goes to. Its limits are the driver model's own, as for every sender."""
    request: type[BaseModel] = type(name, (command.request_model,), {
        "__module__": __name__,
        "__annotations__": {"device_id": str},
        "device_id": Field(..., description="The device, as the topology or its orca-client names it."),
        "model_config": ConfigDict(frozen=True, extra="forbid"),
    })
    return request


def _operation(route: TypedRoute) -> type[TakesTheRunner]:
    kind_path, _, name = route.path.strip("/").partition("/{device_id}/")
    kind = _KIND_OF_PATH[kind_path]
    command = COMMANDS.commands_of(route.interface)[route.command]
    if command.is_property:
        # A driver property is a reading; the device registry holds it.
        raise ValueError(f"{route.path} serves the property {command.name!r}, which is read from the device registry")
    prefix = f"{_CLASS_PREFIX[kind]}{_camel(name)}"

    class TypedCommandOperation:
        Request: ClassVar[type[BaseModel]] = _request_on_a_device(f"{prefix}Request", command)
        Response: ClassVar[type[BaseModel]] = envelope_of(command.response_model)
        binding: ClassVar[RestBinding] = RestBinding(
            method=_method_of(command), path=route.path, tag=kind.tag, drives_device=True,
        )

        def __init__(self, *, devices: IDeviceCommandRunner) -> None:
            self._devices = devices

        async def run(self, req: BaseModel) -> BaseModel:
            device_id = str(req.model_dump()["device_id"])
            await require_kind(self._devices, device_id, kind)
            with answering_for(device_id):
                result = await self._devices.run(
                    device_id, command, command.validate(req.model_dump(mode="json", exclude={"device_id"}, exclude_none=True)),
                )
            check_labware_state(device_id, command.name, result)
            return self.Response.model_validate({"message": f"{command.name} on {device_id}", "result": result})

    TypedCommandOperation.__name__ = TypedCommandOperation.__qualname__ = f"{prefix}Operation"
    TypedCommandOperation.__doc__ = f"Send `{command.name}` to a {kind.label}."
    return TypedCommandOperation


def _segment(command: str) -> str:
    """The path segment a command is served at: kebab-case, and a read drops `get_` because GET says it."""
    return command.removeprefix("get_").replace("_", "-")


def _device(command: str, interface: str) -> TypedRoute:
    return TypedRoute(f"/devices/{{device_id}}/{_segment(command)}", interface, command)


def _arm(command: str) -> TypedRoute:
    return TypedRoute(f"/transporters/{{device_id}}/{_segment(command)}", "ITransporter", command)


def _lh(command: str, interface: str = "ILiquidHandler") -> TypedRoute:
    return TypedRoute(f"/liquid-handlers/{{device_id}}/{_segment(command)}", interface, command)


_THERMOCYCLER_READS = (
    "get_block_current_temperature", "get_block_target_temperature", "get_lid_current_temperature",
    "get_lid_target_temperature", "get_lid_open", "get_lid_status", "get_block_status", "get_hold_time",
    "get_current_cycle_index", "get_total_cycle_count", "get_current_step_index", "get_total_step_count",
)

TYPED_ROUTES: tuple[TypedRoute, ...] = (
    _device("shake", "IShaker"),
    _device("centrifuge", "ICentrifuge"),
    _device("seal", "ISealer"),
    _device("stop", "IShaker"),
    _device("open", "IShaker"),
    _device("close", "IShaker"),
    _device("lock_plate", "IShaker"),
    _device("unlock_plate", "IShaker"),
    _device("dispense", "IStorage"),
    _device("set_temperature", "ITempSettable"),
    _device("get_temperature", "ITempGettable"),
    _device("run_protocol", "IThermocycler"),
    _device("open_lid", "IThermocycler"),
    _device("close_lid", "IThermocycler"),
    _device("set_block_temperature", "IThermocycler"),
    _device("set_lid_temperature", "IThermocycler"),
    _device("deactivate_block", "IThermocycler"),
    _device("deactivate_lid", "IThermocycler"),
    *(_device(name, "IThermocycler") for name in _THERMOCYCLER_READS),
    _arm("home"),
    _arm("move_to_safe"),
    _arm("move_to_coords"),
    _arm("close_gripper"),
    _arm("get_joint_position"),
    _arm("get_cartesian_position"),
    _arm("set_speed"),
    _arm("get_speed"),
    _arm("halt"),
    _arm("move_single_axis"),
    _arm("move_single_axis_relative"),
    _arm("set_free_mode"),
    _lh("get_deck_state"),
    _lh("reconcile_hardware_state"),
    _lh("discard_stranded_tips"),
    _lh("pick_up_tips"),
    _lh("drop_tips"),
    _lh("pick_up_tips96"),
    _lh("drop_tips96"),
    _lh("aspirate96"),
    _lh("dispense96"),
    _lh("discard_tips"),
    _lh("liquid_probe", "ILiquidProbe"),
    _lh("return_tips96"),
    _lh("move_plate"),
)

_BUILT = {route.path: _operation(route) for route in TYPED_ROUTES}
TYPED_COMMAND_OPERATIONS: tuple[type[TakesTheRunner], ...] = tuple(_BUILT.values())


def typed_operation(path: str) -> type[TakesTheRunner]:
    """The Operation served at this path, such as `/liquid-handlers/{device_id}/reconcile-hardware-state`."""
    return _BUILT[path]
