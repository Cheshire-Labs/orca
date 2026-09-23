"""Operations over the state nobody has settled."""

from typing import ClassVar
from pydantic import BaseModel
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.state_models import (
    UnsettledStateRequest,
    UnsettledStateResponse,
    UnsettledSubjectDTO,
)


class UnsettledStateOperation:
    Request: ClassVar[type[BaseModel]] = UnsettledStateRequest
    Response: ClassVar[type[BaseModel]] = UnsettledStateResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: UnsettledStateRequest) -> UnsettledStateResponse:
        del req
        return UnsettledStateResponse(unsettled=[
            UnsettledSubjectDTO.from_subject(item)
            for item in await self._runtime.unsettled_state()
        ])
