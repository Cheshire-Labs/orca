"""Engine-side system-info Operations.

`GetSystemInfoOperation` is the reference Operation. It exercises
every binder (the orca-core daemon REST plus a hosted deployment's REST and MCP surfaces) end-to-end
to validate the framework. The existing daemon `GET /system` route is
untouched -- this binds alongside on `GET /operations/system-info`.
"""

from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from orca.operations._protocol import OperationError
from orca.runtime.facades.registry import IRegistryFacade


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


class GetSystemInfoRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GetSystemInfoResponse(BaseModel):
    """The ``is_simulating`` field is gone because
    run-mode is request-scope (per-submission), not deployment-
    scope. Effective mode per device is exposed via the device-status
    Operations + ``SubmissionDTO.run_mode``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    description: str
    version: str


class GetSystemInfoOperation:
    Request: ClassVar[type[BaseModel]] = GetSystemInfoRequest
    Response: ClassVar[type[BaseModel]] = GetSystemInfoResponse

    def __init__(self, runtime: _RuntimeWithRegistry):
        self._runtime = runtime

    async def run(self, req: GetSystemInfoRequest) -> GetSystemInfoResponse:
        del req
        try:
            snapshot = self._runtime.registry.system_info()
        except AttributeError as exc:
            raise OperationError.service_unavailable(
                "runtime registry not available",
            ) from exc
        return GetSystemInfoResponse(
            name=snapshot.name,
            description=snapshot.description,
            version=snapshot.version,
        )
