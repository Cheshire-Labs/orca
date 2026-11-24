"""A device registry entry, as a server answers `GET /api/devices/registry/{name}`, for CLI tests."""

from orca.operations.wire_models import DeviceRegistryEntryResponse, EngineDeviceStateDTO
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode


def registry_entry(
    name: str, *, type_name: str = "Shaker", position_ids: tuple[str, ...] = (),
    loaded_labware_ids: tuple[str, ...] = (), under_external_control: bool = False,
) -> DeviceRegistryEntryResponse:
    return DeviceRegistryEntryResponse.model_validate({
        "name": name,
        "topology_card": {
            "name": name, "declared_kind": type_name.lower(), "declared_interfaces": [],
            "mounting_locations": list(position_ids), "declared_interfaces_are_class_defaults": False,
        },
        "connection_card": None,
        "is_client_connected": False,
        "is_device_connected": True,
        "is_initialized": True,
        "device_link_mode": "CHECK_RUN",
        "mode_eligibility": {"check_run": True, "sim": True, "live": False},
        "engine": EngineDeviceStateDTO(
            type_name=type_name, is_busy=False, effective_mode=WorkflowRunMode.CHECK_RUN, setting=DeviceMode.SIM,
            position_ids=position_ids, loaded_labware_ids=loaded_labware_ids, held_labware_id=None,
            under_external_control=under_external_control, external_control_hold=None,
        ),
    })
