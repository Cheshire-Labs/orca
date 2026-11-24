"""Read a check run's own store: its plates, where it left them, and their volumes."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.check_run_models import (
    CheckRunLabwareModel,
    GetCheckRunLabwareRequest,
    GetCheckRunLabwareResponse,
)
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.check_runs import CheckRuns, ICheckRunStore
from orca.state.contents import LabwareContentsLedger
from orca.state.ops_history import OpsHistory
from orca.operations.check_run_models import (
    GET_CHECK_RUN_LABWARE_ROUTE,
)


class GetCheckRunLabwareOperation:
    """Every labware a check run's store holds, as the run left it; it is never in the real record."""

    Request: ClassVar[type[BaseModel]] = GET_CHECK_RUN_LABWARE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_CHECK_RUN_LABWARE_ROUTE.response
    binding: ClassVar[RestBinding] = GET_CHECK_RUN_LABWARE_ROUTE.binding

    def __init__(self, check_runs: CheckRuns | None) -> None:
        self._check_runs = check_runs

    async def run(self, req: GetCheckRunLabwareRequest) -> GetCheckRunLabwareResponse:
        if self._check_runs is None:
            raise OperationError.runtime_not_ready("no system loaded")
        store = await self._check_runs.open_store(req.execution_id)
        if store is None:
            raise OperationError.of(
                ErrorCode.CHECK_RUN_NOT_FOUND,
                f"no kept check run has execution {req.execution_id!r}", execution_id=req.execution_id,
            )
        try:
            return GetCheckRunLabwareResponse(execution_id=req.execution_id, labware=await _labware_in(store))
        finally:
            await store.aclose()


async def _labware_in(store: ICheckRunStore) -> list[CheckRunLabwareModel]:
    contents = LabwareContentsLedger(OpsHistory(store.ops_history))
    found: list[CheckRunLabwareModel] = []
    # A labware the run took away is shown where it last stood.
    positions = [*await store.labware.list_active_locations(), *await store.labware.list_departed_locations()]
    for labware_id, position_id in positions:
        instance = await store.labware.get_by_id(labware_id)
        if instance is None:
            continue
        held = await contents.of(instance.ref)
        found.append(CheckRunLabwareModel(
            id=instance.id,
            name=instance.name,
            template_name=instance.template_name,
            labware_type=instance.labware_type,
            barcode=instance.barcode,
            location=position_id,
            well_volumes=held.volumes,
            provenance=held.provenance,
        ))
    return found
