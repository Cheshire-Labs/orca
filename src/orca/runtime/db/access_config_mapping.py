"""DB-neutral mapping between ``AccessConfig`` and ``AccessConfigRow``.

Lives in orca-core and is reused by every per-DB access-config store (orca's
``SqliteAccessConfigStore`` and a hosted deployment's ``PostgresAccessConfigStore``). The
mapping is dialect-independent; it carries no knowledge of which engine is
behind the row.
"""

from typing import Literal

from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.db.models import AccessConfigRow


def access_config_to_row(config: AccessConfig) -> AccessConfigRow:
    return AccessConfigRow(
        name=config.name,
        access_type=config.access_type,
        gripper_offset=config.gripper_offset,
        vertical_clearance=config.vertical_clearance,
        horizontal_clearance=config.horizontal_clearance,
    )


def row_to_access_config(row: AccessConfigRow) -> AccessConfig:
    return AccessConfig(
        name=row.name,
        access_type=_validate_access_type(row.access_type),
        gripper_offset=row.gripper_offset,
        vertical_clearance=row.vertical_clearance,
        horizontal_clearance=row.horizontal_clearance,
    )


def apply_access_config_to_row(row: AccessConfigRow, config: AccessConfig) -> None:
    """Overwrite a row's mutable fields from a config (update path; name fixed)."""
    row.access_type = config.access_type
    row.gripper_offset = config.gripper_offset
    row.vertical_clearance = config.vertical_clearance
    row.horizontal_clearance = config.horizontal_clearance


def _validate_access_type(value: str) -> Literal["vertical", "horizontal"]:
    if value not in ("vertical", "horizontal"):
        raise ValueError(f"Unknown access_type {value!r}")
    return value
