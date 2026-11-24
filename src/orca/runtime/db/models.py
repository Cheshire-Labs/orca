"""ORM models for orca-core's persistence layer.

``JSON`` maps to each engine's JSON column type via SQLAlchemy's
dialect handling, so the same model serves both engines.
"""

from datetime import datetime
from typing import Optional

from pydantic import JsonValue
from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from orca.runtime.db.base import Base, utc_now


class ExecutionRecordRow(Base):
    """Durable copy of one submitted workflow execution; RUNNING -> terminal."""

    __tablename__ = "executions"

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    submitted_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    terminal_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    terminal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ExecutionThreadRow(Base):
    """Last-known summary of one thread, upserted on each of its status changes."""

    __tablename__ = "execution_threads"

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pause_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    group_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class IncidentRow(Base):
    """Durable copy of a SystemIncident.

    ``detail`` holds the frozen category-specific dataclass serialized as
    JSON. ``severity`` and ``recovery_action`` are stored as enum names for
    portability across dialects.
    """

    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, JsonValue]] = mapped_column(JSON, default=dict)
    recovery_action: Mapped[str] = mapped_column(String(32))
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), default=None, index=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    timestamp: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(default=None)


_BigIntAutoPk = BigInteger().with_variant(Integer(), "sqlite")


class LabwareInstanceRow(Base):
    """A labware's identity and where it is now. Its moves are in LabwareEventRow."""

    __tablename__ = "labware_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_name: Mapped[str] = mapped_column(String(255), index=True)
    labware_type: Mapped[str] = mapped_column(String(255), default="unknown")
    barcode: Mapped[Optional[str]] = mapped_column(String(255), default=None, index=True)
    position_id: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), default=None, index=True)
    # How this labware is carried, over every other move-parameter layer. A
    # restart that dropped it would carry a lidded plate as a bare one.
    carry_override: Mapped[Optional[dict[str, JsonValue]]] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)

    __table_args__ = (UniqueConstraint("barcode", name="uix_labware_barcode"),)


class LabwareEventRow(Base):
    """Append-only log of what happened to a labware: registered, relocated, departed."""

    __tablename__ = "labware_events"

    id: Mapped[int] = mapped_column(_BigIntAutoPk, primary_key=True, autoincrement=True)
    labware_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("labware_instances.id", ondelete="CASCADE"), index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict[str, JsonValue]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(default=utc_now, index=True)


class AuditEntryRow(Base):
    """One confirmed audited action. ``id`` orders rows in the order they were written."""

    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(_BigIntAutoPk, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Float, index=True)
    action_name: Mapped[str] = mapped_column(String(128), index=True)
    danger_level: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, default=None)
    call_args: Mapped[dict[str, JsonValue]] = mapped_column(JSON, default=dict)


class OpsHistoryRow(Base):
    """One TrackingRecord. ``payload`` is the whole record; the other columns are filters."""

    __tablename__ = "ops_history"

    id: Mapped[int] = mapped_column(_BigIntAutoPk, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(64), index=True)
    action_id: Mapped[str] = mapped_column(String(64), index=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    method_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    source: Mapped[str] = mapped_column(String(32))
    timestamp: Mapped[float] = mapped_column(Float, index=True)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSON)


class OpsHistoryLabwareRow(Base):
    """Which labware one ops_history record touches, so a search by labware narrows in SQL."""

    __tablename__ = "ops_history_labware"

    id: Mapped[int] = mapped_column(_BigIntAutoPk, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(
        _BigIntAutoPk, ForeignKey("ops_history.id", ondelete="CASCADE"), index=True,
    )
    labware_name: Mapped[str] = mapped_column(String(255), index=True)


class OpsHistoryDeviceRow(Base):
    """Which devices one ops_history record ran on, so a search by device narrows in SQL."""

    __tablename__ = "ops_history_device"

    id: Mapped[int] = mapped_column(_BigIntAutoPk, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(
        _BigIntAutoPk, ForeignKey("ops_history.id", ondelete="CASCADE"), index=True,
    )
    device_name: Mapped[str] = mapped_column(String(255), index=True)


class RuntimeEventRow(Base):
    """One RuntimeEvent. ``id`` is the cursor; ``details`` is the whole event."""

    __tablename__ = "runtime_events"

    id: Mapped[int] = mapped_column(_BigIntAutoPk, primary_key=True, autoincrement=True)
    event_name: Mapped[str] = mapped_column(String(128), index=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(64), default=None, index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    entity_id: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    status: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    timestamp: Mapped[float] = mapped_column(Float)
    details: Mapped[dict[str, JsonValue]] = mapped_column(JSON)


class DeviceModeRow(Base):
    """Whether one device runs as sim or live; a device with no row is sim."""

    __tablename__ = "device_modes"

    device_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    changed_at: Mapped[datetime] = mapped_column(default=utc_now)
