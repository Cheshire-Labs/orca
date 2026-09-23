"""AccessConfigFacade: the external CRUD surface over `IAccessConfigStore`.

REST/MCP/CLI routes through `deployment_registries.access_configs.<method>`
rather than against any standalone repository class. The store instance the
runtime resolves teachpoints against IS the same instance this facade exposes
-- there is exactly one registry per concept per process. Operator edits and
runtime resolution share that single instance.

Reads pass through. Writes inherit whatever safety contract the underlying
store implements: the source-available InMemory store is permissive (deletes succeed
when the name exists); a hosted deployment's DB-backed store enforces protected-name and
in-use checks via `ProtectedAccessConfigError` / `AccessConfigInUseError`.
The facade does not add a second layer of validation; the store is the
source of truth.
"""

from typing import List

from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.interfaces import IAccessConfigStore
from orca.runtime.runtime_interface import IAccessConfigFacade


class AccessConfigFacade(IAccessConfigFacade):
    """Concrete IAccessConfigFacade implementation."""

    def __init__(self, store: IAccessConfigStore) -> None:
        self._store = store

    async def get(self, name: str) -> AccessConfig | None:
        return await self._store.get(name)

    async def list(self) -> List[AccessConfig]:
        return await self._store.list()

    @dangerous(
        name="access_configs.add",
        level=DangerLevel.OPERATOR,
        message="Register a new access config '{config.name}'. New "
                "teachpoints can reference it; existing teachpoints are "
                "unaffected.",
    )
    async def add(self, config: AccessConfig) -> None:
        await self._store.add(config)

    @dangerous(
        name="access_configs.update",
        level=DangerLevel.CRITICAL,
        message="Update access config '{config.name}'. Every teachpoint "
                "referencing this config will resolve to the new values "
                "on the next dispatch lookup.",
    )
    async def update(self, config: AccessConfig) -> None:
        await self._store.update(config)

    @dangerous(
        name="access_configs.delete",
        level=DangerLevel.CRITICAL,
        message="Delete access config '{name}'. Raises if the name is "
                "protected or still referenced by any teachpoint; the "
                "store is the source of truth on both checks.",
    )
    async def delete(self, name: str) -> bool:
        return await self._store.delete(name)
