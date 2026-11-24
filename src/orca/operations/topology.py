"""Read the topology: the live view, or the source it was built from."""

from typing import ClassVar, Protocol

from pydantic import BaseModel

from orca.operations.wire_models import (
    LabwareTemplateDTO,
    LocationDTO,
    MoverDTO,
    ResourcePoolDTO,
    TopologySourceResponse,
    TopologyViewResponse,
    TransporterDTO,
)
from orca.operations.binding import RestBinding
from orca.runtime.registries.device_registry import declared_card
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.topology_models import (
    GET_TOPOLOGY_ROUTE,
)
from orca.operations.topology_models import (
    GetTopologyRequest,
    TopologyAnswer,
)


class ITopologyReader(Protocol):
    def runtime(self) -> ISystemRuntime: ...

    async def topology_source(self) -> TopologySourceResponse: ...


class GetTopologyOperation:
    Request: ClassVar[type[BaseModel]] = GET_TOPOLOGY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_TOPOLOGY_ROUTE.response
    binding: ClassVar[RestBinding] = GET_TOPOLOGY_ROUTE.binding

    def __init__(self, reader: ITopologyReader) -> None:
        self._reader = reader

    async def run(self, req: GetTopologyRequest) -> TopologyAnswer:
        if req.source:
            return TopologyAnswer(await self._reader.topology_source())
        runtime = self._reader.runtime()
        registry = runtime.registry
        transporters = registry.list_transporters()
        transporter_names = {t.name for t in transporters}
        return TopologyAnswer(TopologyViewResponse(
            devices=[
                declared_card(entry) for entry in runtime.topology.list_devices()
                if entry.name not in transporter_names
            ],
            transporters=[TransporterDTO.from_dc(t) for t in transporters],
            movers=[MoverDTO.from_dc(m) for m in registry.list_movers()],
            resource_pools=[ResourcePoolDTO.from_dc(p) for p in registry.list_resource_pools()],
            locations=[LocationDTO.from_dc(loc) for loc in registry.list_locations()],
            labware_templates=[
                LabwareTemplateDTO.from_dc(lt) for lt in registry.list_labware_templates()
            ],
        ))
