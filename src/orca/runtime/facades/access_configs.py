"""AccessConfigFacade: the external CRUD surface over `IAccessConfigStore`.

REST/CLI routes through `deployment_registries.access_configs.<method>`
rather than against any standalone repository class. The store instance the
runtime resolves teachpoints against IS the same instance this facade exposes
-- there is exactly one registry per concept per process. Operator edits and
runtime resolution share that single instance.

Reads pass through. A delete of a config a teachpoint file still names raises
`AccessConfigInUseError` from the store; the facade adds no checks of its own.
"""

from typing import List

from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.danger import DangerLevel, dangerous
from orca.resource_models.store_interfaces import IAccessConfigStore
from orca.runtime.facade_interfaces import IAccessConfigFacade


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
    async def add(self, config: AccessConfig, *, reason: str | None = None) -> None:
        del reason  # consumed by @dangerous audit
        await self._store.add(config)

    @dangerous(
        name="access_configs.update",
        level=DangerLevel.CRITICAL,
        message="Update access config '{config.name}'. Every teachpoint "
                "referencing this config will resolve to the new values "
                "on the next dispatch lookup.",
    )
    async def update(self, config: AccessConfig, *, reason: str | None = None) -> None:
        del reason  # consumed by @dangerous audit
        await self._store.update(config)

    @dangerous(
        name="access_configs.delete",
        level=DangerLevel.CRITICAL,
        message="Delete access config '{name}'. Raises if a teachpoint "
                "still names it.",
    )
    async def delete(self, name: str, *, reason: str | None = None) -> bool:
        del reason  # consumed by @dangerous audit
        return await self._store.delete(name)
