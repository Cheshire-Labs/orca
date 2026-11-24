"""Whether the run can proceed, and everything standing in its way."""

from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from orca.operations.wire_models import RuntimeStatusResponse
from orca.operations.binding import RestBinding
from orca.operations.blocker_models import BlockersPayload
from orca.operations.route_tags import RouteTag
from orca.operations.binding import RestBinding, Route


class IRuntimeBuildState(Protocol):
    def is_built(self) -> bool: ...

    def last_build_error(self) -> dict[str, str] | None: ...

    async def blockers(self) -> BlockersPayload: ...


class GetRuntimeStatusRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


GET_RUNTIME_STATUS_ROUTE = Route(
    GetRuntimeStatusRequest,
    RuntimeStatusResponse,
    RestBinding(
        method="GET", path="/runtime/status", tag=RouteTag.RUNTIME,
    ),
)


class GetRuntimeStatusOperation:
    Request: ClassVar[type[BaseModel]] = GET_RUNTIME_STATUS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_RUNTIME_STATUS_ROUTE.response
    binding: ClassVar[RestBinding] = GET_RUNTIME_STATUS_ROUTE.binding

    def __init__(self, state: IRuntimeBuildState) -> None:
        self._state = state

    async def run(self, req: GetRuntimeStatusRequest) -> RuntimeStatusResponse:
        del req
        built = self._state.is_built()
        payload = await self._state.blockers()
        return RuntimeStatusResponse(
            built=built,
            last_build_error=self._state.last_build_error(),
            blockers=payload.blockers,
            blocker_count=payload.blocker_count,
            blockers_known=payload.blockers_known,
            blockers_fingerprint=payload.blockers_fingerprint,
        )
