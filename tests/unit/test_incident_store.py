"""Unit tests for IncidentService over a SqliteIncidentStore (:memory:).

``record``/``acknowledge`` are synchronous enqueues drained on-loop; tests call
``flush_pending()`` to drain before reading the DB (the source of truth).
"""

import pytest

from orca.runtime.db import create_memory_engine
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    AutoSpawnFailedDetail,
    BarcodeMismatchDetail,
    CoLabwareTimeoutDetail,
    DeadlockDetail,
    DeviceBusyExhaustedDetail,
    DeviceInitFailedDetail,
    EventHandlerExceptionDetail,
    IncidentCategory,
    IncidentDetail,
    IncidentSeverity,
    OtherIncidentDetail,
    PluginHandlerExceptionDetail,
    RecoverableTimeoutContext,
    RecoveryAction,
    SystemIncident,
    UnresolvedAnchorInsertDetail,
    VariableResolutionDetail,
    VariableValidationDetail,
)
from orca.runtime.sqlite_incident_store import SqliteIncidentStore


async def _service() -> IncidentService:
    svc = IncidentService(SqliteIncidentStore(create_memory_engine()))
    await svc.ensure_schema()
    return svc


def _co_labware() -> CoLabwareTimeoutDetail:
    return CoLabwareTimeoutDetail(
        missing_labware_names=("tips_96",),
        waited_seconds=60.0,
        position_id="incubator-bay-3",
    )


def _variable_resolution() -> VariableResolutionDetail:
    return VariableResolutionDetail(var_name="temperature", action_command="shake")


async def test_record_returns_incident_and_stores_it() -> None:
    service = await _service()
    detail = _co_labware()
    rec = service.record(
        category=IncidentCategory.CO_LABWARE_TIMEOUT,
        severity=IncidentSeverity.WARNING,
        message="Missing tips_96 at incubator-bay-3",
        detail=detail,
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        execution_id="exec-1",
        thread_id="thread-plate_1",
    )
    assert isinstance(rec, SystemIncident)
    assert rec.category == IncidentCategory.CO_LABWARE_TIMEOUT
    assert rec.severity == IncidentSeverity.WARNING
    assert rec.detail == detail
    assert rec.acknowledged is False
    await service.flush_pending()
    fetched = await service.get(rec.id)
    assert fetched == rec


async def test_get_missing_raises() -> None:
    service = await _service()
    with pytest.raises(KeyError):
        await service.get("not-a-real-id")


async def test_list_filters_by_category_execution_and_since() -> None:
    service = await _service()
    a = service.record(
        category=IncidentCategory.VARIABLE_RESOLUTION,
        severity=IncidentSeverity.ERROR,
        message="a",
        detail=_variable_resolution(),
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        execution_id="exec-1",
    )
    b = service.record(
        category=IncidentCategory.CO_LABWARE_TIMEOUT,
        severity=IncidentSeverity.WARNING,
        message="b",
        detail=_co_labware(),
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        execution_id="exec-2",
    )
    c = service.record(
        category=IncidentCategory.VARIABLE_RESOLUTION,
        severity=IncidentSeverity.ERROR,
        message="c",
        detail=_variable_resolution(),
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        execution_id="exec-1",
    )
    await service.flush_pending()

    by_cat = await service.list(category=IncidentCategory.VARIABLE_RESOLUTION)
    assert {i.id for i in by_cat} == {a.id, c.id}

    by_exec = await service.list(execution_id="exec-2")
    assert [i.id for i in by_exec] == [b.id]

    since_future = await service.list(since=b.timestamp + 1_000_000)
    assert since_future == []


async def test_acknowledge_marks_record_and_is_idempotent() -> None:
    service = await _service()
    rec = service.record(
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.INFO,
        message="x",
        detail=OtherIncidentDetail(message_extra="..."),
        recovery_action=RecoveryAction.NONE,
    )
    assert rec.acknowledged is False
    await service.flush_pending()

    service.acknowledge(rec.id)
    await service.flush_pending()
    acked = await service.get(rec.id)
    assert acked.acknowledged is True
    assert acked.id == rec.id
    # The originally-returned rec is a frozen snapshot; reading the acked row
    # returns a fresh object, so our original instance never mutates.
    assert rec.acknowledged is False

    # Idempotent: acking again leaves it acked.
    service.acknowledge(rec.id)
    await service.flush_pending()
    acked2 = await service.get(rec.id)
    assert acked2.acknowledged is True
    assert acked2.id == rec.id


async def test_acknowledge_all_counts_and_filters() -> None:
    service = await _service()
    for _ in range(3):
        service.record(
            category=IncidentCategory.VARIABLE_RESOLUTION,
            severity=IncidentSeverity.ERROR,
            message="v",
            detail=_variable_resolution(),
            recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        )
    for _ in range(2):
        service.record(
            category=IncidentCategory.CO_LABWARE_TIMEOUT,
            severity=IncidentSeverity.WARNING,
            message="c",
            detail=_co_labware(),
            recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        )
    await service.flush_pending()

    # Ack only the variable-resolution ones.
    count = await service.acknowledge_all(category=IncidentCategory.VARIABLE_RESOLUTION)
    assert count == 3

    unack = await service.list(unacknowledged_only=True)
    assert len(unack) == 2
    assert all(i.category == IncidentCategory.CO_LABWARE_TIMEOUT for i in unack)


async def test_list_unacknowledged_only() -> None:
    service = await _service()
    r1 = service.record(
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.INFO,
        message="1",
        detail=OtherIncidentDetail(message_extra=""),
        recovery_action=RecoveryAction.NONE,
    )
    r2 = service.record(
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.INFO,
        message="2",
        detail=OtherIncidentDetail(message_extra=""),
        recovery_action=RecoveryAction.NONE,
    )
    await service.flush_pending()
    service.acknowledge(r1.id)
    await service.flush_pending()

    unack_ids = [i.id for i in await service.list(unacknowledged_only=True)]
    assert unack_ids == [r2.id]


async def test_typed_details_are_retained_without_serialization_loss() -> None:
    """Each typed detail dataclass survives storage + retrieval byte-for-byte."""
    service = await _service()
    cases: list[tuple[IncidentCategory, IncidentDetail]] = [
        (IncidentCategory.VARIABLE_RESOLUTION, VariableResolutionDetail(var_name="foo", action_command="bar")),
        (IncidentCategory.VARIABLE_VALIDATION, VariableValidationDetail(var_name="x", offered_value="42", expected_type="int", reason="too big")),
        (IncidentCategory.CO_LABWARE_TIMEOUT, CoLabwareTimeoutDetail(missing_labware_names=("a", "b"), waited_seconds=30.0, position_id="loc")),
        (IncidentCategory.AUTO_SPAWN_FAILED, AutoSpawnFailedDetail(requested_labware_name="tips", requesting_thread_id="t1")),
        (IncidentCategory.BARCODE_MISMATCH, BarcodeMismatchDetail(expected_barcode="ABC", scanned_barcode=None, context="join")),
        (IncidentCategory.RESERVATION_DEADLOCK, DeadlockDetail(cycling_thread_ids=("t1", "t2"), yielding_thread_id="t1", reroute_applied=True)),
        (IncidentCategory.RECOVERABLE_TIMEOUT, RecoverableTimeoutContext(device_id="shaker_1", command="shake", command_id="cmd-abc", elapsed_seconds=7300.0, max_seconds=7200.0)),
        (IncidentCategory.DEVICE_INIT_FAILED, DeviceInitFailedDetail(device_name="shaker-1", driver_error="timeout")),
        (IncidentCategory.DEVICE_BUSY_EXHAUSTED, DeviceBusyExhaustedDetail(device_name="shaker-1", retry_count=5, last_error="busy")),
        (IncidentCategory.PLUGIN_HANDLER_EXCEPTION, PluginHandlerExceptionDetail(plugin_type_name="MethodTracker", event_name="METHOD.COMPLETED", exception_type="RuntimeError", exception_message="boom")),
        (IncidentCategory.EVENT_HANDLER_EXCEPTION, EventHandlerExceptionDetail(handler_name="h1", event_name="THREAD.PAUSED", exception_type="ValueError", exception_message="bad")),
        (IncidentCategory.UNRESOLVED_ANCHOR_INSERT, UnresolvedAnchorInsertDetail(anchor_name="seal", direction="before", target_type="method", item_name="custom_wash", anchor_reached=True)),
        (IncidentCategory.OTHER, OtherIncidentDetail(message_extra="misc")),
    ]

    recorded = []
    for category, detail in cases:
        rec = service.record(
            category=category,
            severity=IncidentSeverity.INFO,
            message=f"msg-{category.name}",
            detail=detail,
            recovery_action=RecoveryAction.NONE,
        )
        recorded.append((rec, detail))
    await service.flush_pending()

    for rec, original_detail in recorded:
        fetched = await service.get(rec.id)
        assert fetched.detail == original_detail
        assert fetched.category == rec.category


async def test_list_is_sorted_by_timestamp() -> None:
    service = await _service()
    ids = []
    for i in range(5):
        rec = service.record(
            category=IncidentCategory.OTHER,
            severity=IncidentSeverity.INFO,
            message=str(i),
            detail=OtherIncidentDetail(message_extra=str(i)),
            recovery_action=RecoveryAction.NONE,
        )
        ids.append(rec.id)
    await service.flush_pending()
    out = await service.list()
    assert [i.id for i in out] == ids


def test_incident_enums_are_str_enum_with_name_value_parity() -> None:
    """G1c characterization: enums are (str, Enum) so .name == .value == str(member.value).

    Wire safety: callers that JSON-dump these enums or stamp .name into DTOs
    must see the same uppercase identifier. Member equality with the raw
    string is intentional (str subclass).
    """
    for member in IncidentCategory:
        assert member.value == member.name
        assert isinstance(member.value, str)
        assert member == member.value
    for member in IncidentSeverity:
        assert member.value == member.name
        assert isinstance(member.value, str)
        assert member == member.value
    for member in RecoveryAction:
        assert member.value == member.name
        assert isinstance(member.value, str)
        assert member == member.value


def test_incident_enum_lookup_by_name_still_works() -> None:
    """Lookup paths in routes/api/mcp use EnumClass[name]; pin that it works."""
    assert IncidentCategory["CO_LABWARE_TIMEOUT"] is IncidentCategory.CO_LABWARE_TIMEOUT
    assert IncidentSeverity["WARNING"] is IncidentSeverity.WARNING
    assert RecoveryAction["NONE"] is RecoveryAction.NONE


def test_incident_dto_wire_shape_unchanged_after_str_enum() -> None:
    """G1c characterization: IncidentDTO carries enum names as strings on the wire."""
    from orca.daemon.schemas import IncidentDTO

    inc = SystemIncident(
        id="abc",
        timestamp=0.0,
        category=IncidentCategory.CO_LABWARE_TIMEOUT,
        severity=IncidentSeverity.WARNING,
        execution_id="exec-1",
        thread_id="thr-1",
        message="msg",
        detail=_co_labware(),
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        acknowledged=False,
    )
    dto = IncidentDTO.from_incident(inc)
    dumped = dto.model_dump()
    assert dumped["category"] == "CO_LABWARE_TIMEOUT"
    assert dumped["severity"] == "WARNING"
    assert dumped["recovery_action"] == "THREAD_RECOVER_RETRY"
    assert isinstance(dumped["category"], str)
    assert isinstance(dumped["severity"], str)
    assert isinstance(dumped["recovery_action"], str)


def test_incident_dto_tuple_detail_field_serializes_as_json_list() -> None:
    """Regression: detail dataclasses with ``tuple[str, ...]`` fields must
    convert to JSON lists in IncidentDTO. ``detail`` is typed
    ``dict[str, JsonValue]`` (no tuple type), so ``from_incident`` must
    normalize the ``dataclasses.asdict`` tuples to lists or construction
    raises a pydantic ValidationError (invalid-json-value)."""
    from orca.daemon.schemas import IncidentDTO

    inc = SystemIncident(
        id="abc",
        timestamp=0.0,
        category=IncidentCategory.CO_LABWARE_TIMEOUT,
        severity=IncidentSeverity.WARNING,
        execution_id="exec-1",
        thread_id="thr-1",
        message="msg",
        detail=CoLabwareTimeoutDetail(
            missing_labware_names=("tips_96", "plate_1"),
            waited_seconds=60.0,
            position_id="bay-3",
        ),
        recovery_action=RecoveryAction.THREAD_RECOVER_RETRY,
        acknowledged=False,
    )
    dto = IncidentDTO.from_incident(inc)
    assert dto.detail["missing_labware_names"] == ["tips_96", "plate_1"]
    # mode="json" is the actual wire form; it must round-trip cleanly.
    assert dto.model_dump(mode="json")["detail"]["missing_labware_names"] == [
        "tips_96",
        "plate_1",
    ]
