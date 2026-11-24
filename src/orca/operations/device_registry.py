"""The one device list: declared devices and transporters plus connected ones, each with what the engine holds."""

import dataclasses
from typing import ClassVar

from pydantic import BaseModel

from orca.operations.wire_models import (
    DeviceFaultModel,
    DeviceModeEligibilityDTO,
    DeviceRegistryEntryResponse,
    DeviceRegistryListResponse,
    EngineDeviceStateDTO,
)
from orca.gateway.operator_commands import fault_summary
from orca.operations.binding import RestBinding
from orca.operations.device_commands import device_not_found
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.status_models import DeviceRegistryEntry
from orca.operations.device_registry_models import (
    LIST_DEVICE_REGISTRY_ROUTE,
    SHOW_DEVICE_REGISTRY_ENTRY_ROUTE,
)
from orca.operations.device_registry_models import (
    ListDeviceRegistryRequest,
    ShowDeviceRegistryEntryRequest,
)


async def registry_view(
    runtime: ISystemRuntime, entry: DeviceRegistryEntry,
) -> DeviceRegistryEntryResponse:
    summary = fault_summary(runtime.device_faults, entry.name)
    engine = await runtime.registry.engine_state(entry.name)
    return DeviceRegistryEntryResponse(
        name=entry.name,
        topology_card=entry.topology_card,
        connection_card=entry.connection_card,
        is_client_connected=await entry.is_client_connected(),
        is_device_connected=await entry.is_device_connected(),
        is_initialized=await entry.is_initialized(),
        device_link_mode=entry.device_link_mode(),
        mode_eligibility=DeviceModeEligibilityDTO(
            check_run=await entry.supports_mode(WorkflowRunMode.CHECK_RUN),
            sim=await entry.supports_mode(WorkflowRunMode.SIM),
            live=await entry.supports_mode(WorkflowRunMode.LIVE),
        ),
        fault=DeviceFaultModel.model_validate(dataclasses.asdict(summary)) if summary is not None else None,
        engine=EngineDeviceStateDTO.from_dc(engine) if engine is not None else None,
        interface_mismatch=entry.interface_mismatch,
    )


class ListDeviceRegistryOperation:
    Request: ClassVar[type[BaseModel]] = LIST_DEVICE_REGISTRY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_DEVICE_REGISTRY_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_DEVICE_REGISTRY_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: ListDeviceRegistryRequest) -> DeviceRegistryListResponse:
        del req
        entries = await self._runtime.device_registry.list_all()
        return DeviceRegistryListResponse(devices=[await registry_view(self._runtime, e) for e in entries])


class ShowDeviceRegistryEntryOperation:
    Request: ClassVar[type[BaseModel]] = SHOW_DEVICE_REGISTRY_ENTRY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SHOW_DEVICE_REGISTRY_ENTRY_ROUTE.response
    binding: ClassVar[RestBinding] = SHOW_DEVICE_REGISTRY_ENTRY_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: ShowDeviceRegistryEntryRequest) -> DeviceRegistryEntryResponse:
        entry = await self._runtime.device_registry.get(req.device_id)
        if entry is None:
            raise device_not_found(req.device_id)
        return await registry_view(self._runtime, entry)


DEVICE_REGISTRY_OPERATIONS = (ListDeviceRegistryOperation, ShowDeviceRegistryEntryOperation)
