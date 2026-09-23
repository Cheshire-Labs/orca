"""Tests for daemon DTOs that actually catch bugs.

Three things worth asserting here, since most of the "does the field land in
the DTO" work is Pydantic doing its job:

1. **Structural drift.** For every (dataclass, DTO) pair in the MVP surface,
   the DTO's field names must exactly equal the dataclass's field names. If
   someone adds a field to the dataclass but forgets the DTO (or vice versa),
   this is a loud failure.

2. **Enum coercion.** `ExecutionState` is an Enum in the dataclass; we need
   its string `value` on the wire, not its repr. This tests the
   `use_enum_values=True` config choice.

3. **Wire-format stability.** A realistic payload must serialize to an exact
   JSON shape. Clients depend on this wire format; changes here break them.
"""

import dataclasses
import json

import pytest
from pydantic import ValidationError

from orca.daemon.schemas import (
    ActionSnapshotDTO,
    DeviceDTO,
    ExecutionDetailDTO,
    ExecutionRecordDTO,
    LabwareDTO,
    LabwareTemplateDTO,
    LocationDTO,
    LocationEventDTO,
    MethodSnapshotDTO,
    MethodTemplateDTO,
    ReservationSnapshotDTO,
    ResourcePoolDTO,
    SystemInfoDTO,
    ThreadSnapshotDTO,
    TransporterDTO,
    WorkflowTemplateDTO,
)
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.status_models import (
    ActionSnapshot,
    DeviceSnapshot,
    ExecutionDetail,
    LabwareSnapshot,
    LabwareTemplateSnapshot,
    LocationEvent,
    LocationSnapshot,
    MethodSnapshot,
    MethodTemplateSnapshot,
    ReservationSnapshot,
    ResourcePoolSnapshot,
    SystemInfoSnapshot,
    ThreadSnapshot,
    TransporterSnapshot,
    WorkflowTemplateSnapshot,
)


_DC_DTO_PAIRS = [
    (ActionSnapshot, ActionSnapshotDTO),
    (MethodSnapshot, MethodSnapshotDTO),
    (ThreadSnapshot, ThreadSnapshotDTO),
    (ReservationSnapshot, ReservationSnapshotDTO),
    (ExecutionDetail, ExecutionDetailDTO),
    (ExecutionRecord, ExecutionRecordDTO),
    # Registry DTOs
    (SystemInfoSnapshot, SystemInfoDTO),
    (WorkflowTemplateSnapshot, WorkflowTemplateDTO),
    (MethodTemplateSnapshot, MethodTemplateDTO),
    (LocationSnapshot, LocationDTO),
    (DeviceSnapshot, DeviceDTO),
    # Topology projection DTOs (C3 widening)
    (TransporterSnapshot, TransporterDTO),
    (ResourcePoolSnapshot, ResourcePoolDTO),
    (LabwareTemplateSnapshot, LabwareTemplateDTO),
    # Labware DTOs
    (LabwareSnapshot, LabwareDTO),
    (LocationEvent, LocationEventDTO),
]


@pytest.mark.parametrize(("dc_cls", "dto_cls"), _DC_DTO_PAIRS)
def test_dto_fields_match_dataclass_fields(
    dc_cls: type, dto_cls: type,
) -> None:
    """Every (dataclass, DTO) pair must have exactly matching field names."""
    dc_fields = {f.name for f in dataclasses.fields(dc_cls)}
    dto_fields = set(dto_cls.model_fields)
    missing_in_dto = dc_fields - dto_fields
    extra_in_dto = dto_fields - dc_fields
    assert not missing_in_dto, (
        f"{dto_cls.__name__} is missing fields from {dc_cls.__name__}: {missing_in_dto}"
    )
    assert not extra_in_dto, (
        f"{dto_cls.__name__} has fields not on {dc_cls.__name__}: {extra_in_dto}"
    )


def test_extra_keys_are_rejected() -> None:
    """extra='forbid' must fail fast on stray keys, not silently drop them."""
    with pytest.raises(ValidationError, match="extra_forbidden|Extra"):
        ReservationSnapshotDTO.model_validate(
            {
                "position_id": "loc1",
                "reservation_id": "rsv-1",
                "thread_id": "t1",
                "injected_future_field": "surprise",
            }
        )


def test_execution_state_serializes_to_value_string() -> None:
    """ExecutionRecord.status is an ExecutionState enum on the dataclass.

    The DTO attribute is a plain string -- the
    Operations surface emits the richer ExecutionPhase vocabulary
    (accepting / draining / ...) alongside the four ExecutionState
    values, and pinning the attribute to the narrow enum would reject
    valid wire payloads from the live runtime. The wire serialization
    is still the lowercase value string in both directions.
    """
    rec = ExecutionRecord(
        id="exec-2", workflow_name="smc",
        status=ExecutionState.RUNNING, error=None,
    )
    dto = ExecutionRecordDTO.from_dc(rec)
    assert dto.status == "running"
    payload = json.loads(dto.model_dump_json())
    assert payload["status"] == "running"
    assert isinstance(payload["status"], str)


def test_execution_detail_wire_format() -> None:
    """Exact JSON wire format for a realistic nested payload (client contract)."""
    action = ActionSnapshot(
        id="a1", command="shake", status="RUNNING",
        position_id="shaker1", resource_name="shaker_device",
        description="Shake the plate for two hours at 875 rpm.",
    )
    method = MethodSnapshot(
        id="m1", name="shake_method", status="RUNNING",
        current_action=action, completed_action_count=0,
    )
    thread = ThreadSnapshot(
        id="t1", name="plate_thread", status="RUNNING",
        current_location="shaker1", current_method=method,
        completed_method_count=0, last_error=None, pause_reason=None,
        completed_methods=(),
    )
    detail = ExecutionDetail(
        id="exec-1", workflow_name="smc", status="running", error=None,
        threads=[thread],
        total_thread_count=1, completed_thread_count=0, active_thread_count=1,
    )
    dto = ExecutionDetailDTO.from_dc(detail)

    expected = {
        "id": "exec-1",
        "workflow_name": "smc",
        "status": "running",
        "error": None,
        "threads": [
            {
                "id": "t1",
                "name": "plate_thread",
                "status": "RUNNING",
                "current_location": "shaker1",
                "current_method": {
                    "id": "m1",
                    "name": "shake_method",
                    "status": "RUNNING",
                    "current_action": {
                        "id": "a1",
                        "command": "shake",
                        "status": "RUNNING",
                        "position_id": "shaker1",
                        "resource_name": "shaker_device",
                        "description": "Shake the plate for two hours at 875 rpm.",
                    },
                    "completed_action_count": 0,
                },
                "completed_method_count": 0,
                "last_error": None,
                "pause_reason": None,
                "completed_methods": [],
                "labware_template_name": None,
                "labware_id": None,
                "labware_name": None,
                "paused_device_command": None,
                "pause_message": None,
                "pause_site": None,
                "honoured_decisions": [],
                "waiting_for": None,
            },
        ],
        "total_thread_count": 1,
        "completed_thread_count": 0,
        "active_thread_count": 1,
        "paused": False,
        "pause_reason": None,
        "abort_armed": False,
    }
    actual = json.loads(dto.model_dump_json())
    assert actual == expected


# -- FailurePolicy wire-shape characterization -------------------------------
#
# Pins the externally-observable shape of `FailurePolicy` so the underlying
# enum kind (`Enum + auto()` vs `(str, Enum)`) can change without breaking
# wire or in-process callers:
#
# - JSON wire form: the `.name` string ("PAUSE" / "ABORT").
# - In-process attribute: still a `FailurePolicy` enum (identity + equality).
# - Validator input: accepts both the enum instance and the name string.
# - `.name` attribute: still the member name string.
#
# This is intentionally redundant with `test_routes_registry::
# test_catalog_methods_carries_failure_policy` so the contract is pinned at
# the schema layer too (no httpx fixture required).


def test_failure_policy_method_template_dto_wire_is_name_string() -> None:
    """`MethodTemplateDTO.failure_policy` rides as the .name string on JSON."""
    from orca.workflow_models.status_enums import FailurePolicy

    snap = MethodTemplateSnapshot(
        workflow_name="assay", name="shake_2hr", failure_policy=FailurePolicy.PAUSE,
    )
    dto = MethodTemplateDTO.from_dc(snap)

    assert dto.failure_policy is FailurePolicy.PAUSE
    payload = json.loads(dto.model_dump_json())
    assert payload == {"workflow_name": "assay", "name": "shake_2hr", "failure_policy": "PAUSE"}


def test_failure_policy_method_template_dto_abort_wire() -> None:
    """ABORT round-trips through the validator and serializes to its name."""
    from orca.workflow_models.status_enums import FailurePolicy

    snap = MethodTemplateSnapshot(
        workflow_name="assay", name="read_endpoint", failure_policy=FailurePolicy.ABORT,
    )
    dto = MethodTemplateDTO.from_dc(snap)
    assert dto.failure_policy is FailurePolicy.ABORT
    payload = json.loads(dto.model_dump_json())
    assert payload == {"workflow_name": "assay", "name": "read_endpoint", "failure_policy": "ABORT"}


def test_failure_policy_validator_accepts_name_string() -> None:
    """Inbound wire form -- the .name string -- coerces to the enum."""
    from orca.workflow_models.status_enums import FailurePolicy

    dto = MethodTemplateDTO.model_validate(
        {"workflow_name": "assay", "name": "x", "failure_policy": "PAUSE"},
    )
    assert dto.failure_policy is FailurePolicy.PAUSE

    dto2 = MethodTemplateDTO.model_validate(
        {"workflow_name": "assay", "name": "y", "failure_policy": "ABORT"},
    )
    assert dto2.failure_policy is FailurePolicy.ABORT


def test_failure_policy_validator_accepts_enum_instance() -> None:
    """In-process callers pass the enum directly; validator must accept it."""
    from orca.workflow_models.status_enums import FailurePolicy

    dto = MethodTemplateDTO.model_validate(
        {"workflow_name": "assay", "name": "x", "failure_policy": FailurePolicy.PAUSE},
    )
    assert dto.failure_policy is FailurePolicy.PAUSE


def test_failure_policy_validator_rejects_unknown_string() -> None:
    """Names outside the enum fail loudly rather than silently coercing."""
    with pytest.raises(ValidationError):
        MethodTemplateDTO.model_validate(
            {"workflow_name": "assay", "name": "x", "failure_policy": "NOPE"},
        )


def test_failure_policy_name_attribute_matches_member() -> None:
    """`.name` keeps the canonical member identifier regardless of enum kind."""
    from orca.workflow_models.status_enums import FailurePolicy

    assert FailurePolicy.PAUSE.name == "PAUSE"
    assert FailurePolicy.ABORT.name == "ABORT"


def test_failure_policy_members_unchanged() -> None:
    """Member set is the contract. Adding/removing members breaks this test."""
    from orca.workflow_models.status_enums import FailurePolicy

    assert {m.name for m in FailurePolicy} == {"PAUSE", "ABORT"}


def test_teachpoint_dto_emits_flat_coords() -> None:
    """`coords` carries axis values only; `coord_type`/`orientation` ride at the
    top level. A nested `type` key (the pre-flatten shape) diverged from orca's
    `typed_to_wire` and a hosted REST surface, which are flat.
    """
    from cheshire_drivers.teachpoints import (
        CartesianCoordinates,
        JointCoordinates,
        Teachpoint,
    )
    from orca.daemon.schemas import TeachpointDTO

    cartesian = Teachpoint(
        position_id="home",
        coordinates=CartesianCoordinates(
            x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=0.0, roll=0.0,
        ),
        orientation="left",
        access=None,
        gateway=None,
    )
    cart_dto = TeachpointDTO.from_value("robot1", cartesian)
    assert cart_dto.coord_type == "cartesian"
    assert "type" not in cart_dto.coords
    assert "orientation" not in cart_dto.coords
    assert cart_dto.coords["x"] == 1.0

    joint = Teachpoint(
        position_id="park",
        coordinates=JointCoordinates(
            rail=1.0, base=2.0, shoulder=3.0, elbow=4.0, wrist=5.0, gripper=6.0,
        ),
        orientation=None,
        access=None,
        gateway=None,
    )
    joint_dto = TeachpointDTO.from_value("robot1", joint)
    assert joint_dto.coord_type == "joint"
    assert "type" not in joint_dto.coords
    assert joint_dto.coords["gripper"] == 6.0


def test_cli_thread_snapshot_dto_stays_field_equal_to_the_daemon_one() -> None:
    """``orca.cli.control_plane.ThreadSnapshotDTO`` claims field-equality with
    the daemon schema. It is ``extra="ignore"``, so drift is silent: a field
    added on the daemon side simply never reaches the CLI."""
    from orca.cli.control_plane import ThreadSnapshotDTO as CliThreadSnapshotDTO

    assert set(CliThreadSnapshotDTO.model_fields) == set(ThreadSnapshotDTO.model_fields)
