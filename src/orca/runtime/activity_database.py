"""What the lab does, kept in one database per deployment.

Plate identities and positions, run records, incidents, the operation record,
audit rows and events live here, so a restart keeps them. The daemon chooses a SQLite file in its package
folder when it starts (`orca.daemon.app`).
"""

from pathlib import Path
from typing import Protocol

from orca.runtime.audit_store import IAuditStore
from orca.runtime.event_store import IEventStore
from orca.runtime.startup import IActivityServices


def state_db_path(package_root: Path) -> Path:
    return package_root / ".orca" / "state.db"


class IActivityStores(IActivityServices, Protocol):
    """The activity services plus the audit and event stores, opened and closed with the server."""

    @property
    def audit(self) -> IAuditStore: ...

    @property
    def events(self) -> IEventStore: ...

    async def open(self) -> None: ...

    async def aclose(self) -> None: ...
