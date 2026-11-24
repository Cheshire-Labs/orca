"""Each device's setup file, `devices/<name>.json`. Served with or without a runtime."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.device_setup_models import DeviceSetupResponse, GetDeviceSetupRequest, PutDeviceSetupRequest
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.resource_models.devices import Device
from orca.resource_models.site_rack import SiteRack
from orca.resource_models.transporter import Transporter
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.setup_files import SetupFileError
from orca.system.system_interface import ISystem
from orca.operations.device_setup_models import (
    GET_DEVICE_SETUP_ROUTE,
    PUT_DEVICE_SETUP_ROUTE,
)

def _invalid(exc: ValueError | SetupFileError, device_name: str) -> OperationError:
    return OperationError.of(
        ErrorCode.VALIDATION_ERROR, str(exc), device_name=device_name,
    )


class GetDeviceSetupOperation:
    """The device's setup as its file holds it. A device with no file reads as the default sim."""

    Request: ClassVar[type[BaseModel]] = GET_DEVICE_SETUP_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_DEVICE_SETUP_ROUTE.response
    binding: ClassVar[RestBinding] = GET_DEVICE_SETUP_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._setups = registries.device_setups

    async def run(self, req: GetDeviceSetupRequest) -> DeviceSetupResponse:
        try:
            setup = self._setups.read(req.device_id)
        except (ValueError, SetupFileError) as exc:
            raise _invalid(exc, req.device_id) from exc
        return DeviceSetupResponse(device_name=req.device_id, setup=setup)


class PutDeviceSetupOperation:
    """Write the device's whole setup file. With a system loaded, a setup its next load would refuse is refused now."""

    Request: ClassVar[type[BaseModel]] = PUT_DEVICE_SETUP_ROUTE.request
    Response: ClassVar[type[BaseModel]] = PUT_DEVICE_SETUP_ROUTE.response
    binding: ClassVar[RestBinding] = PUT_DEVICE_SETUP_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries, system: ISystem | None) -> None:
        self._setups = registries.device_setups
        self._system = system

    async def run(self, req: PutDeviceSetupRequest) -> DeviceSetupResponse:
        resources = None if self._system is None else [
            r for r in self._system.resources if isinstance(r, Device | Transporter | SiteRack)
        ]
        try:
            await self._setups.put(req.device_id, req.setup, resources, reason=req.reason, confirm=True)
        except ValueError as exc:
            raise _invalid(exc, req.device_id) from exc
        return DeviceSetupResponse(device_name=req.device_id, setup=req.setup)
