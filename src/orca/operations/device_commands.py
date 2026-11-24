"""What a device can run, and the error every surface answers a device failure with."""

from typing import ClassVar

from cheshire_drivers.driver_introspection import MethodInfo
from pydantic import BaseModel, JsonValue

from orca.gateway.controller.exceptions import (
    CommandExecutionError,
    CommandOutcomeUnknownError,
    CommandNotSavedError,
    CommandTimeoutError,
    ConfirmationRequiredError,
    DeviceError,
    DeviceFaultedError,
    DeviceNotBroughtUpError,
    DeviceLockedError,
    DeviceOfflineError,
    DeviceUnknownError,
    InvalidCommandError,
    ModeUnresolvableError,
)
from orca.operations.binding import RestBinding
from orca.resource_models.device_error import DeviceUnderExternalControlError
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.operations.wire_models import (
    DeviceIntrospectionDTO,
)
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.device_models import (
    GET_DEVICE_INTROSPECTION_ROUTE,
)
from orca.operations.device_models import (
    GetDeviceIntrospectionRequest,
)

_DEVICE_ERRORS: dict[type[DeviceError], ErrorCode] = {
    DeviceUnknownError: ErrorCode.DEVICE_NOT_FOUND,
    DeviceOfflineError: ErrorCode.DEVICE_OFFLINE,
    DeviceLockedError: ErrorCode.DEVICE_LOCKED,
    DeviceFaultedError: ErrorCode.DEVICE_FAULTED,
    DeviceNotBroughtUpError: ErrorCode.DEVICE_NOT_BROUGHT_UP,
    ConfirmationRequiredError: ErrorCode.CONFIRMATION_REQUIRED,
    ModeUnresolvableError: ErrorCode.RUNTIME_NOT_READY,
    InvalidCommandError: ErrorCode.VALIDATION_ERROR,
    CommandTimeoutError: ErrorCode.TIMEOUT,
    CommandExecutionError: ErrorCode.COMMAND_ERROR,
    CommandNotSavedError: ErrorCode.COMMAND_NOT_SAVED,
}

def _route_and_url(binding: RestBinding, device_id: str) -> tuple[str, str]:
    """The route as `send_with.rest` names it, by its template, and the url for this device."""
    url, _ = binding.fill_url({"device_id": device_id})
    return f"{binding.method} {binding.url}", f"{binding.method} {url}"


class LifecycleCommandByNameError(InvalidCommandError):
    """connect, initialize or disconnect sent as a command by name; each has an Operation of its own."""

    def __init__(self, command: str, device_id: str, binding: RestBinding) -> None:
        self.rest, url = _route_and_url(binding, device_id)
        self.cli: str | None = f"orca device {command} {device_id}"
        super().__init__(
            f"{command!r} on {device_id!r} has its own Operation, which brings the deck in line after it. "
            f"Send it with {url}, or on the command line with `{self.cli}`."
        )


class ClaimedCommandByNameError(InvalidCommandError):
    """A command an Operation claims, sent by name: sent that way it would skip the Operation's checks."""

    def __init__(self, command: str, device_id: str, binding: RestBinding) -> None:
        self.rest, url = _route_and_url(binding, device_id)
        self.cli: str | None = None
        super().__init__(
            f"{command!r} on {device_id!r} has its own Operation, which checks it before it is sent. "
            f"Send it with {url}."
        )


def device_error_code(exc: DeviceError) -> ErrorCode:
    """The code a device failure answers with on every surface."""
    # Walked, not looked up: a subclass means the same thing to an operator.
    for cls in type(exc).__mro__:
        found = _DEVICE_ERRORS.get(cls)
        if found is not None:
            return found
    return ErrorCode.COMMAND_ERROR


def device_error(exc: DeviceError, device_name: str | None = None) -> OperationError:
    """The error a device failure answers with; `device_name` names the device when the caller knows it."""
    extras: dict[str, JsonValue] = {} if device_name is None else {"device_id": device_name}
    if isinstance(exc, CommandExecutionError):
        extras["driver_error_type"] = exc.error_type
    if isinstance(exc, CommandOutcomeUnknownError):
        extras["incident_id"] = exc.incident_id
    if isinstance(exc, (LifecycleCommandByNameError, ClaimedCommandByNameError)):
        extras["send_with"] = {"rest": exc.rest, "cli": exc.cli}
    return OperationError.of(device_error_code(exc), message_of(exc), **extras)


def external_control_error(exc: DeviceUnderExternalControlError) -> OperationError:
    return OperationError.of(
        ErrorCode.DEVICE_UNDER_EXTERNAL_CONTROL, message_of(exc), device_name=exc.device_name,
    )


def device_not_found(device_id: str) -> OperationError:
    """The one answer every route gives a device no topology declares and no orca-client has connected."""
    return OperationError.of(
        ErrorCode.DEVICE_NOT_FOUND, f"Device {device_id!r} not found", device_id=device_id,
    )


class GetDeviceIntrospectionOperation:
    """What a device can run, with each command's danger level. Answers in simulation and before the device connects."""

    Request: ClassVar[type[BaseModel]] = GET_DEVICE_INTROSPECTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_DEVICE_INTROSPECTION_ROUTE.response
    binding: ClassVar[RestBinding] = GET_DEVICE_INTROSPECTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: GetDeviceIntrospectionRequest) -> DeviceIntrospectionDTO:
        try:
            info = self._runtime.devices.get_device_introspection(req.device_id)
            commands = self._runtime.devices.get_supported_commands(req.device_id)
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        return DeviceIntrospectionDTO(
            type=info.type,
            name=info.name,
            interfaces=info.interfaces,
            capabilities=info.capabilities,
            provides_state=info.provides_state,
            methods={name: MethodInfo.model_validate(method) for name, method in info.methods.items()},
            danger_levels={
                self._runtime.devices.command_name(req.device_id, c.capability): c.danger_level.name
                for c in commands
            },
        )


DEVICE_COMMAND_READ_OPERATIONS = (GetDeviceIntrospectionOperation,)
