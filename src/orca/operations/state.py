"""Operations over the state nobody has settled."""

from orca.operations.binding import RestBinding
from typing import ClassVar
from pydantic import BaseModel
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.state_models import (
    UnsettledStateRequest,
    UnsettledStateResponse,
    UnsettledSubjectDTO,
)
from orca.operations.state_models import (
    UNSETTLED_STATE_ROUTE,
)


class UnsettledStateOperation:
    Request: ClassVar[type[BaseModel]] = UNSETTLED_STATE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UNSETTLED_STATE_ROUTE.response
    binding: ClassVar[RestBinding] = UNSETTLED_STATE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: UnsettledStateRequest) -> UnsettledStateResponse:
        del req
        return UnsettledStateResponse(unsettled=[
            UnsettledSubjectDTO.from_subject(item)
            for item in await self._runtime.unsettled_state()
        ])
