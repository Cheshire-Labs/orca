"""System-info Operations."""

from orca.operations.binding import RestBinding
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.facades.registry import IRegistryFacade
from orca.operations.system_models import (
    GET_SYSTEM_INFO_ROUTE,
)
from orca.operations.system_models import (
    GetSystemInfoRequest,
    GetSystemInfoResponse,
)


@runtime_checkable
class _RuntimeWithRegistry(Protocol):
    """The narrow capability this Operation needs.

    Decoupling the parameter type from `ISystemRuntime` keeps Operations
    honest about their dependencies (compositional service
    injection) and avoids spurious pyright variance complaints on the
    broader `ISystemRuntime` Protocol.
    """

    @property
    def registry(self) -> IRegistryFacade: ...


class GetSystemInfoOperation:
    Request: ClassVar[type[BaseModel]] = GET_SYSTEM_INFO_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_SYSTEM_INFO_ROUTE.response
    binding: ClassVar[RestBinding] = GET_SYSTEM_INFO_ROUTE.binding

    def __init__(self, runtime: _RuntimeWithRegistry):
        self._runtime = runtime

    async def run(self, req: GetSystemInfoRequest) -> GetSystemInfoResponse:
        del req
        try:
            snapshot = self._runtime.registry.system_info()
        except AttributeError as exc:
            raise OperationError.of(
                ErrorCode.SERVICE_NOT_READY,
                "runtime registry not available",
            ) from exc
        return GetSystemInfoResponse(
            name=snapshot.name,
            description=snapshot.description,
            version=snapshot.version,
            device_count=snapshot.device_count,
            transporter_count=snapshot.transporter_count,
            workflow_count=snapshot.workflow_count,
            method_count=snapshot.method_count,
        )
