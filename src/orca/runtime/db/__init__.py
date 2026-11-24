"""orca-core persistence layer: engine-agnostic SQLAlchemy foundation."""

from orca.runtime.db.base import Base, utc_now
from orca.runtime.db.engine import (
    apply_sqlite_pragmas,
    create_all_tables,
    create_blocking_sqlite_engine,
    create_tables,
    create_memory_engine,
    create_sqlite_engine,
    make_session_factory,
)
from orca.runtime.db.models import (
    ExecutionRecordRow,
    IncidentRow,
)

__all__ = [
    "Base",
    "utc_now",
    "apply_sqlite_pragmas",
    "create_all_tables",
    "create_tables",
    "create_blocking_sqlite_engine",
    "create_memory_engine",
    "create_sqlite_engine",
    "make_session_factory",
    "ExecutionRecordRow",
    "IncidentRow",
]
