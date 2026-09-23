"""DB-neutral mapping between ``SystemIncident`` and ``IncidentRow``.

Lives in orca-core and is reused by every per-DB incident store (orca's
``SqliteIncidentStore`` and a hosted deployment's ``PostgresIncidentStore``). Mapping a
domain object to a shared table schema is dialect-independent; it carries no
knowledge of which engine is behind the row.
"""

import dataclasses

from pydantic import BaseModel, ConfigDict, JsonValue

from orca.runtime.db.models import IncidentRow
from orca.runtime.incident_store import (
    AutoSpawnFailedDetail,
    BarcodeMismatchDetail,
    CoLabwareTimeoutDetail,
    DeadlockDetail,
    DeckReconcileConflictDetail,
    LedgerContradictionDetail,
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
    SystemStallDetail,
    UnresolvedAnchorInsertDetail,
    VariableResolutionDetail,
    VariableValidationDetail,
)
from orca.system.reservation_manager.errors import (
    ActionContinuedContext,
    ActionFailedContext,
    MoveContinuedContext,
    MoveFailedContext,
    OrphanedBacklogContext,
    ThreadDiedContext,
    UnresolvableDeadlockContext,
)


class _VariableResolutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    var_name: str
    action_command: str | None = None


class _VariableValidationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    var_name: str
    offered_value: str
    expected_type: str
    reason: str


class _CoLabwareTimeoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    missing_labware_names: list[str]
    waited_seconds: float
    position_id: str


class _AutoSpawnFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_labware_name: str
    requesting_thread_id: str


class _BarcodeMismatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_barcode: str
    scanned_barcode: str | None = None
    context: str


class _DeadlockPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycling_thread_ids: list[str]
    yielding_thread_id: str
    reroute_applied: bool


class _UnresolvableDeadlockPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requesting_thread_id: str
    requesting_labware_id: str
    blocking_position_id: str
    blocking_thread_id: str
    blocking_labware_id: str
    reason: str
    hint: str


class _SystemStallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stalled_thread_ids: list[str]
    waits: list[str]


class _OrphanedBacklogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_key: str
    labware_template_name: str
    receiver_thread_id: str
    receiver_thread_name: str
    receiver_status: str
    undelivered_count: int
    in_flight_method_name: str | None
    pause_requested_thread_ids: list[str]


class _DeviceInitFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str
    driver_error: str


class _DeviceBusyExhaustedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str
    retry_count: int
    last_error: str


class _PluginHandlerExceptionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_type_name: str
    event_name: str
    exception_type: str
    exception_message: str


class _EventHandlerExceptionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handler_name: str
    event_name: str
    exception_type: str
    exception_message: str


class _UnresolvedAnchorInsertPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_name: str
    direction: str
    target_type: str
    item_name: str
    # Absent on rows written before the field existed. False matches the
    # message those rows already carry; the old code could not tell.
    anchor_reached: bool = False


class _ActionFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_command: str
    method_name: str
    error_type: str
    error_message: str
    # Defaulted so rows written before the field existed still rehydrate.
    device_command: str | None = None


class _ThreadDiedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_name: str
    labware_name: str
    labware_id: str
    last_position_id: str | None
    error_type: str
    error_message: str


class _ActionContinuedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_command: str
    method_name: str
    error_type: str
    error_message: str


class _MoveFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    transporter: str
    labware: str
    error_type: str
    error_message: str


class _MoveContinuedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    transporter: str
    labware: str
    error_type: str
    error_message: str


class _RecoverableTimeoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str
    command: str
    command_id: str
    elapsed_seconds: float
    max_seconds: float


class _OtherIncidentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_extra: str


class _LedgerContradictionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str
    command: str
    labware_id: str | None = None
    labware_name: str
    positions: list[str]
    believed: str


class _DeckReconcileConflictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str
    labware_id: str
    labware_name: str
    position_id: str
    # Defaulted so a row written before conflicts carried a reason still loads,
    # as the layout case is the only one that could have produced one.
    reason: str = "site_not_in_layout"
    driver_site: str | None = None
    blocking_labware_name: str | None = None
    detail: str | None = None


def detail_to_json(detail: IncidentDetail) -> dict[str, JsonValue]:
    if isinstance(detail, CoLabwareTimeoutDetail):
        return {
            "missing_labware_names": list(detail.missing_labware_names),
            "waited_seconds": detail.waited_seconds,
            "position_id": detail.position_id,
        }
    if isinstance(detail, DeadlockDetail):
        return {
            "cycling_thread_ids": list(detail.cycling_thread_ids),
            "yielding_thread_id": detail.yielding_thread_id,
            "reroute_applied": detail.reroute_applied,
        }
    if isinstance(detail, SystemStallDetail):
        return {
            "stalled_thread_ids": list(detail.stalled_thread_ids),
            "waits": list(detail.waits),
        }
    if isinstance(detail, OrphanedBacklogContext):
        return {
            "slot_key": detail.slot_key,
            "labware_template_name": detail.labware_template_name,
            "receiver_thread_id": detail.receiver_thread_id,
            "receiver_thread_name": detail.receiver_thread_name,
            "receiver_status": detail.receiver_status,
            "undelivered_count": detail.undelivered_count,
            "in_flight_method_name": detail.in_flight_method_name,
            "pause_requested_thread_ids": list(detail.pause_requested_thread_ids),
        }
    if isinstance(detail, UnresolvableDeadlockContext):
        return {
            "requesting_thread_id": detail.requesting_thread_id,
            "requesting_labware_id": detail.requesting_labware_id,
            "blocking_position_id": detail.blocking_position_id,
            "blocking_thread_id": detail.blocking_thread_id,
            "blocking_labware_id": detail.blocking_labware_id,
            "reason": detail.reason,
            "hint": detail.hint,
        }
    return dataclasses.asdict(detail)


def json_to_detail(
    category: IncidentCategory, payload: dict[str, JsonValue]
) -> IncidentDetail:
    if category is IncidentCategory.VARIABLE_RESOLUTION:
        parsed = _VariableResolutionPayload.model_validate(payload)
        return VariableResolutionDetail(
            var_name=parsed.var_name, action_command=parsed.action_command
        )
    if category is IncidentCategory.VARIABLE_VALIDATION:
        parsed_v = _VariableValidationPayload.model_validate(payload)
        return VariableValidationDetail(
            var_name=parsed_v.var_name,
            offered_value=parsed_v.offered_value,
            expected_type=parsed_v.expected_type,
            reason=parsed_v.reason,
        )
    if category is IncidentCategory.CO_LABWARE_TIMEOUT:
        parsed_c = _CoLabwareTimeoutPayload.model_validate(payload)
        return CoLabwareTimeoutDetail(
            missing_labware_names=tuple(parsed_c.missing_labware_names),
            waited_seconds=parsed_c.waited_seconds,
            position_id=parsed_c.position_id,
        )
    if category is IncidentCategory.AUTO_SPAWN_FAILED:
        parsed_a = _AutoSpawnFailedPayload.model_validate(payload)
        return AutoSpawnFailedDetail(
            requested_labware_name=parsed_a.requested_labware_name,
            requesting_thread_id=parsed_a.requesting_thread_id,
        )
    if category is IncidentCategory.BARCODE_MISMATCH:
        parsed_b = _BarcodeMismatchPayload.model_validate(payload)
        return BarcodeMismatchDetail(
            expected_barcode=parsed_b.expected_barcode,
            scanned_barcode=parsed_b.scanned_barcode,
            context=parsed_b.context,
        )
    if category is IncidentCategory.RESERVATION_DEADLOCK:
        parsed_d = _DeadlockPayload.model_validate(payload)
        return DeadlockDetail(
            cycling_thread_ids=tuple(parsed_d.cycling_thread_ids),
            yielding_thread_id=parsed_d.yielding_thread_id,
            reroute_applied=parsed_d.reroute_applied,
        )
    if category is IncidentCategory.UNRESOLVABLE_DEADLOCK:
        parsed_ud = _UnresolvableDeadlockPayload.model_validate(payload)
        return UnresolvableDeadlockContext(
            requesting_thread_id=parsed_ud.requesting_thread_id,
            requesting_labware_id=parsed_ud.requesting_labware_id,
            blocking_position_id=parsed_ud.blocking_position_id,
            blocking_thread_id=parsed_ud.blocking_thread_id,
            blocking_labware_id=parsed_ud.blocking_labware_id,
            reason=parsed_ud.reason,
            hint=parsed_ud.hint,
        )
    if category is IncidentCategory.SYSTEM_STALL:
        parsed_ss = _SystemStallPayload.model_validate(payload)
        return SystemStallDetail(
            stalled_thread_ids=tuple(parsed_ss.stalled_thread_ids),
            waits=tuple(parsed_ss.waits),
        )
    if category is IncidentCategory.ORPHANED_BACKLOG:
        parsed_ob = _OrphanedBacklogPayload.model_validate(payload)
        return OrphanedBacklogContext(
            slot_key=parsed_ob.slot_key,
            labware_template_name=parsed_ob.labware_template_name,
            receiver_thread_id=parsed_ob.receiver_thread_id,
            receiver_thread_name=parsed_ob.receiver_thread_name,
            receiver_status=parsed_ob.receiver_status,
            undelivered_count=parsed_ob.undelivered_count,
            in_flight_method_name=parsed_ob.in_flight_method_name,
            pause_requested_thread_ids=tuple(parsed_ob.pause_requested_thread_ids),
        )
    if category is IncidentCategory.DEVICE_INIT_FAILED:
        parsed_di = _DeviceInitFailedPayload.model_validate(payload)
        return DeviceInitFailedDetail(
            device_name=parsed_di.device_name, driver_error=parsed_di.driver_error
        )
    if category is IncidentCategory.DEVICE_BUSY_EXHAUSTED:
        parsed_be = _DeviceBusyExhaustedPayload.model_validate(payload)
        return DeviceBusyExhaustedDetail(
            device_name=parsed_be.device_name,
            retry_count=parsed_be.retry_count,
            last_error=parsed_be.last_error,
        )
    if category is IncidentCategory.PLUGIN_HANDLER_EXCEPTION:
        parsed_ph = _PluginHandlerExceptionPayload.model_validate(payload)
        return PluginHandlerExceptionDetail(
            plugin_type_name=parsed_ph.plugin_type_name,
            event_name=parsed_ph.event_name,
            exception_type=parsed_ph.exception_type,
            exception_message=parsed_ph.exception_message,
        )
    if category is IncidentCategory.EVENT_HANDLER_EXCEPTION:
        parsed_eh = _EventHandlerExceptionPayload.model_validate(payload)
        return EventHandlerExceptionDetail(
            handler_name=parsed_eh.handler_name,
            event_name=parsed_eh.event_name,
            exception_type=parsed_eh.exception_type,
            exception_message=parsed_eh.exception_message,
        )
    if category is IncidentCategory.UNRESOLVED_ANCHOR_INSERT:
        parsed_ua = _UnresolvedAnchorInsertPayload.model_validate(payload)
        return UnresolvedAnchorInsertDetail(
            anchor_name=parsed_ua.anchor_name,
            direction=parsed_ua.direction,
            target_type=parsed_ua.target_type,
            item_name=parsed_ua.item_name,
            anchor_reached=parsed_ua.anchor_reached,
        )
    if category is IncidentCategory.ACTION_FAILED:
        parsed_af = _ActionFailedPayload.model_validate(payload)
        return ActionFailedContext(
            action_command=parsed_af.action_command,
            method_name=parsed_af.method_name,
            error_type=parsed_af.error_type,
            error_message=parsed_af.error_message,
            device_command=parsed_af.device_command,
        )
    if category is IncidentCategory.THREAD_DIED:
        parsed_td = _ThreadDiedPayload.model_validate(payload)
        return ThreadDiedContext(
            thread_name=parsed_td.thread_name,
            labware_name=parsed_td.labware_name,
            labware_id=parsed_td.labware_id,
            last_position_id=parsed_td.last_position_id,
            error_type=parsed_td.error_type,
            error_message=parsed_td.error_message,
        )
    if category is IncidentCategory.ACTION_CONTINUED:
        parsed_ac = _ActionContinuedPayload.model_validate(payload)
        return ActionContinuedContext(
            action_command=parsed_ac.action_command,
            method_name=parsed_ac.method_name,
            error_type=parsed_ac.error_type,
            error_message=parsed_ac.error_message,
        )
    if category is IncidentCategory.MOVE_FAILED:
        parsed_mf = _MoveFailedPayload.model_validate(payload)
        return MoveFailedContext(
            source=parsed_mf.source,
            target=parsed_mf.target,
            transporter=parsed_mf.transporter,
            labware=parsed_mf.labware,
            error_type=parsed_mf.error_type,
            error_message=parsed_mf.error_message,
        )
    if category is IncidentCategory.MOVE_CONTINUED:
        parsed_mc = _MoveContinuedPayload.model_validate(payload)
        return MoveContinuedContext(
            source=parsed_mc.source,
            target=parsed_mc.target,
            transporter=parsed_mc.transporter,
            labware=parsed_mc.labware,
            error_type=parsed_mc.error_type,
            error_message=parsed_mc.error_message,
        )
    if category is IncidentCategory.RECOVERABLE_TIMEOUT:
        parsed_rt = _RecoverableTimeoutPayload.model_validate(payload)
        return RecoverableTimeoutContext(
            device_id=parsed_rt.device_id,
            command=parsed_rt.command,
            command_id=parsed_rt.command_id,
            elapsed_seconds=parsed_rt.elapsed_seconds,
            max_seconds=parsed_rt.max_seconds,
        )
    if category is IncidentCategory.DECK_RECONCILE_CONFLICT:
        parsed_dc = _DeckReconcileConflictPayload.model_validate(payload)
        return DeckReconcileConflictDetail(
            device_name=parsed_dc.device_name,
            labware_id=parsed_dc.labware_id,
            labware_name=parsed_dc.labware_name,
            position_id=parsed_dc.position_id,
            reason=parsed_dc.reason,
            driver_site=parsed_dc.driver_site,
            blocking_labware_name=parsed_dc.blocking_labware_name,
            detail=parsed_dc.detail,
        )
    if category is IncidentCategory.LEDGER_CONTRADICTED:
        parsed_lc = _LedgerContradictionPayload.model_validate(payload)
        return LedgerContradictionDetail(
            device_name=parsed_lc.device_name,
            command=parsed_lc.command,
            labware_id=parsed_lc.labware_id,
            labware_name=parsed_lc.labware_name,
            positions=list(parsed_lc.positions),
            believed=parsed_lc.believed,
        )
    if category is IncidentCategory.OTHER:
        parsed_o = _OtherIncidentPayload.model_validate(payload)
        return OtherIncidentDetail(message_extra=parsed_o.message_extra)
    raise ValueError(f"Unknown incident category: {category!r}")


def row_to_incident(row: IncidentRow) -> SystemIncident:
    category = IncidentCategory[row.category]
    return SystemIncident(
        id=row.id,
        timestamp=row.timestamp,
        category=category,
        severity=IncidentSeverity[row.severity],
        execution_id=row.execution_id,
        thread_id=row.thread_id,
        message=row.message,
        detail=json_to_detail(category, dict(row.detail)),
        recovery_action=RecoveryAction[row.recovery_action],
        acknowledged=row.acknowledged,
    )


def incident_to_row(incident: SystemIncident) -> IncidentRow:
    return IncidentRow(
        id=incident.id,
        category=incident.category.name,
        severity=incident.severity.name,
        message=incident.message,
        detail=detail_to_json(incident.detail),
        recovery_action=incident.recovery_action.name,
        execution_id=incident.execution_id,
        thread_id=incident.thread_id,
        acknowledged=incident.acknowledged,
        timestamp=incident.timestamp,
    )
