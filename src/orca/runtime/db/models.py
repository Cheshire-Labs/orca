"""ORM models for orca-core's persistence layer.

``JSON`` maps to JSONB on Postgres and TEXT on SQLite via SQLAlchemy's
dialect handling, so the same model serves both engines.
"""

from datetime import datetime
from typing import Optional

from pydantic import JsonValue
from sqlalchemy import Boolean, Float, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from orca.runtime.db.base import Base, utc_now


class TeachpointRow(Base):
    """Durable copy of a cheshire-drivers Teachpoint for one transporter.

    The store is per-transporter, so ``position_id`` alone is the primary
    key (no device_id column; the store instance is the device scope).
    ``coords`` holds the coordinate dict whose shape ``coord_type`` selects;
    both are nullable for coordinate-less waypoints. Flattened access fields
    are stored inline so the store round-trips a teachpoint without an
    AccessConfig lookup; ``access_config_name`` preserves the FK name so a
    re-read reconstructs the named ``AccessConfig`` the topology declared.
    """

    __tablename__ = "teachpoints"

    position_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    coord_type: Mapped[Optional[str]] = mapped_column(String(16), default=None)
    coords: Mapped[Optional[dict[str, JsonValue]]] = mapped_column(JSON, default=None)
    orientation: Mapped[Optional[str]] = mapped_column(String(16), default=None)
    gateway: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    access_config_name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    access_type: Mapped[Optional[str]] = mapped_column(String(16), default=None)
    gripper_offset: Mapped[Optional[float]] = mapped_column(Float, default=None)
    vertical_clearance: Mapped[Optional[float]] = mapped_column(Float, default=None)
    horizontal_clearance: Mapped[Optional[float]] = mapped_column(Float, default=None)
    taught_with: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    by_labware: Mapped[Optional[dict[str, JsonValue]]] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class DeckLayoutRow(Base):
    """Durable copy of a DeckLayoutConfig for one liquid handler.

    The store is per-liquid-handler, so ``name`` alone is the primary key
    (no device_id column; the store instance is the device scope). The whole
    config is persisted as one JSON blob in ``deck_data``.
    """

    __tablename__ = "deck_layouts"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    deck_data: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class LabwareCatalogRow(Base):
    """Durable copy of a LabwareCatalogEntry (one labware definition).

    ``labware_type`` is the primary key: the catalog is a deployment-wide
    registry keyed by labware_type. ``geometry`` holds the full
    ``LabwareSeedEntry`` dump as one JSON blob; the denormalized
    ``display_name``/``category``/``vendor``/``plr_class_name`` columns are the
    list-surface facets. ``source`` is ``plr_seed`` or ``operator_custom``.
    """

    __tablename__ = "labware_definitions"

    labware_type: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64))
    vendor: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    source: Mapped[str] = mapped_column(String(32))
    geometry: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    plr_class_name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class ExecutionRecordRow(Base):
    """Durable copy of one submitted workflow execution; RUNNING -> terminal."""

    __tablename__ = "executions"

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_name: Mapped[str] = mapped_column(String(255), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    terminal_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    terminal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ExecutionThreadRow(Base):
    """Last-known summary of one thread, upserted from THREAD lifecycle events."""

    __tablename__ = "execution_threads"

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pause_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class AccessConfigRow(Base):
    """Durable copy of a cheshire-drivers AccessConfig.

    ``name`` is the primary key: access configs are a deployment-wide registry
    keyed by name, and CRUD operates on the name (add raises on duplicate,
    get/update/delete take the name).
    """

    __tablename__ = "access_configs"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    access_type: Mapped[str] = mapped_column(String(16))
    gripper_offset: Mapped[float] = mapped_column(Float)
    vertical_clearance: Mapped[float] = mapped_column(Float)
    horizontal_clearance: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class MoveDefaultsRow(Base):
    """What this deployment has tuned about one transporter's moves.

    One row per transporter, because a safe retreat margin or a jaw opening is
    a property of the arm rather than of the deployment.

    The blob is a sparse ``MoveParameterPatch``, holding only the fields somebody
    set. Storing a total record instead would freeze the untouched fields at
    whatever the seed said the day the row was written, and would make an
    operator's number indistinguishable from one nobody ever chose.
    """

    __tablename__ = "move_defaults"

    transporter_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    patch: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class GripProfileRow(Base):
    """How one labware type is held, as a sparse patch over the arm's defaults.

    Keyed by labware type rather than by labware instance: a tip box and a deep
    well plate grip at different heights and want different jaw widths wherever
    they are, and that is a property of the type. Sparse because a type usually
    corrects one or two numbers and inherits the rest; a total record here would
    freeze every other field at whatever it happened to resolve to on the day the
    row was written.

    A sibling of the labware catalog rather than a column on it, because seeded
    catalog rows are read-only and a grip width is exactly what an operator needs
    to correct for the types their deck actually carries.
    """

    __tablename__ = "grip_profiles"

    labware_type: Mapped[str] = mapped_column(String(255), primary_key=True)
    patch: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


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
