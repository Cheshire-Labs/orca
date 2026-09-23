"""Wire-shape contract tests for topology-related daemon DTOs.

Pins the exact JSON shape of the five topology-projection DTOs
(``DeviceDTO``, ``TransporterDTO``, ``ResourcePoolDTO``, ``LocationDTO``,
``LabwareTemplateDTO``) so a hosted deployment (and any other client that imports the
canonical DTOs) sees a stable contract.

Pairs with ``test_schemas.py``'s parametrized field-set guard: that test
catches "DTO drifted from dataclass"; these tests catch "wire format
changed without anyone noticing".
"""

import dataclasses
import json

from orca.daemon.schemas import (
    DeviceDTO,
    LabwareTemplateDTO,
    LocationDTO,
    ResourcePoolDTO,
    TransporterDTO,
)
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import (
    DeviceSnapshot,
    LabwareTemplateSnapshot,
    LocationSnapshot,
    ResourcePoolSnapshot,
    TransporterSnapshot,
)


def test_device_dto_wire_shape_pins_all_projection_fields() -> None:
    """DeviceDTO must surface every field a hosted deployment previously projected.

    ``loaded_labware_ids`` and ``position_ids`` are the projection fields
    a hosted deployment's TopologyDeviceDTO added on top of the underlying DeviceSnapshot;
    both must round-trip through the canonical DTO.
    """
    snap = DeviceSnapshot(
        name="shaker1",
        type_name="Shaker",
        is_initialized=True,
        is_busy=False,
        effective_mode=WorkflowRunMode.PURE_SIM,
        position_ids=("loc-A", "loc-B"),
        loaded_labware_ids=("lw-1", "lw-2"),
    )
    dto = DeviceDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload == {
        "name": "shaker1",
        "type_name": "Shaker",
        "is_initialized": True,
        "is_busy": False,
        "effective_mode": "PURE_SIM",
        "position_ids": ["loc-A", "loc-B"],
        "loaded_labware_ids": ["lw-1", "lw-2"],
        # S5b external-control flag: default False when no gateway holds
        # the device. Always emitted on the wire so MCP/REST clients have
        # a stable field shape to render against.
        "under_external_control": False,
        "external_control_hold": None,
        # Null until a command leaves the device part-way through something.
        # Always emitted so a client has a stable shape to render against.
        "fault": None,
    }


def test_transporter_dto_wire_shape() -> None:
    """TransporterDTO must mirror TransporterSnapshot byte-for-byte."""
    snap = TransporterSnapshot(
        name="arm-1",
        type_name="Transporter",
        is_busy=True,
        position_ids=("loc-X", "loc-Y"),
        current_labware_id="lw-7",
    )
    dto = TransporterDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload == {
        "name": "arm-1",
        "type_name": "Transporter",
        "is_busy": True,
        "position_ids": ["loc-X", "loc-Y"],
        "current_labware_id": "lw-7",
        # S5b external-control flag: defaults to False so existing
        # ``TransporterSnapshot(...)`` constructors don't need updating.
        "under_external_control": False,
        "external_control_hold": None,
    }


def test_transporter_dto_handles_empty_gripper() -> None:
    """``current_labware_id`` is None when the transporter is empty; that
    must serialize to JSON null, not be dropped."""
    snap = TransporterSnapshot(
        name="arm-1",
        type_name="Transporter",
        is_busy=False,
        position_ids=(),
        current_labware_id=None,
    )
    dto = TransporterDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload["current_labware_id"] is None


def test_resource_pool_dto_wire_shape() -> None:
    """ResourcePoolDTO must mirror ResourcePoolSnapshot byte-for-byte."""
    snap = ResourcePoolSnapshot(
        name="washer-pool",
        member_names=("washer-1", "washer-2", "washer-3"),
        available_count=2,
    )
    dto = ResourcePoolDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload == {
        "name": "washer-pool",
        "member_names": ["washer-1", "washer-2", "washer-3"],
        "available_count": 2,
    }


def test_location_dto_wire_shape_pins_projection_fields() -> None:
    """LocationDTO must surface the loaded_labware_ids a hosted deployment projects."""
    snap = LocationSnapshot(
        name="stacker-1",
        resource_name="stacker_device",
        loaded_labware_ids=("lw-3", "lw-4", "lw-5"),
    )
    dto = LocationDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload == {
        "name": "stacker-1",
        "resource_name": "stacker_device",
        "loaded_labware_ids": ["lw-3", "lw-4", "lw-5"],
        "deck_sites": [],
    }


def test_location_dto_handles_no_mounted_resource() -> None:
    """``resource_name`` is None for unmounted locations and must round-trip
    as JSON null."""
    snap = LocationSnapshot(
        name="bench-1",
        resource_name=None,
        loaded_labware_ids=(),
    )
    dto = LocationDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload == {
        "name": "bench-1",
        "resource_name": None,
        "loaded_labware_ids": [],
        "deck_sites": [],
    }


def test_location_dto_surfaces_deck_sites() -> None:
    """A liquid handler's addressable deck sites ride on the listing DTO so
    REST / CLI / MCP all reveal them without a separate deck-layout call."""
    snap = LocationSnapshot(
        name="mlstar_1",
        resource_name="mlstar_1",
        loaded_labware_ids=(),
        deck_sites=("mlstar_1/carrier-9-0", "mlstar_1/carrier-7-0"),
    )
    dto = LocationDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload == {
        "name": "mlstar_1",
        "resource_name": "mlstar_1",
        "loaded_labware_ids": [],
        "deck_sites": ["mlstar_1/carrier-9-0", "mlstar_1/carrier-7-0"],
    }


def test_labware_template_dto_wire_shape() -> None:
    """LabwareTemplateDTO must mirror LabwareTemplateSnapshot byte-for-byte."""
    snap = LabwareTemplateSnapshot(
        name="plate_1",
        type_name="MicroPlate96Well",
    )
    dto = LabwareTemplateDTO.from_dc(snap)
    payload = json.loads(dto.model_dump_json())
    assert payload == {
        "name": "plate_1",
        "type_name": "MicroPlate96Well",
    }


def test_topology_dtos_match_their_dataclass_field_sets() -> None:
    """Field-set parity: every dataclass field has a DTO field and vice versa.

    Catches drift the moment a snapshot dataclass gains a field but the DTO
    is not updated (or the reverse). Mirrors the parametrized guard in
    ``test_schemas.py`` but with all five topology DTOs in one fixture so a
    breakage shows up as a single assertion failure.
    """
    pairs = [
        (DeviceSnapshot, DeviceDTO),
        (TransporterSnapshot, TransporterDTO),
        (ResourcePoolSnapshot, ResourcePoolDTO),
        (LocationSnapshot, LocationDTO),
        (LabwareTemplateSnapshot, LabwareTemplateDTO),
    ]
    for dc_cls, dto_cls in pairs:
        dc_fields = {f.name for f in dataclasses.fields(dc_cls)}
        dto_fields = set(dto_cls.model_fields)
        assert dc_fields == dto_fields, (
            f"{dto_cls.__name__} field set {dto_fields} does not equal "
            f"{dc_cls.__name__} field set {dc_fields}"
        )
