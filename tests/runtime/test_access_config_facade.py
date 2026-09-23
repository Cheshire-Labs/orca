"""AccessConfigFacade contract:

- Reads pass through to the underlying store.
- Writes pass through; the store is the source of truth on safety checks.
- Exceptions raised by the store (`ProtectedAccessConfigError`,
  `AccessConfigInUseError`) propagate cleanly so REST/MCP can translate.
"""

from typing import Iterable, List

import pytest
from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.access_config_service import AccessConfigService
from orca.runtime.access_config_store import (
    AccessConfigInUseError,
    ProtectedAccessConfigError,
)
from orca.runtime.db import create_memory_engine
from orca.runtime.facades.access_configs import AccessConfigFacade
from orca.runtime.sqlite_access_config_store import SqliteAccessConfigStore


def _cfg(name: str, gripper_offset: float = 10.0) -> AccessConfig:
    return AccessConfig(
        name=name,
        access_type="vertical",
        gripper_offset=gripper_offset,
        vertical_clearance=20.0,
        horizontal_clearance=30.0,
    )


async def _service(seed: Iterable[AccessConfig] = ()) -> AccessConfigService:
    service = AccessConfigService(SqliteAccessConfigStore(create_memory_engine()))
    for cfg in seed:
        await service.add(cfg)
    return service


class TestAccessConfigFacade:

    async def test_get_returns_none_when_absent(self) -> None:
        facade = AccessConfigFacade(await _service())
        assert await facade.get("missing") is None

    async def test_get_returns_stored_value(self) -> None:
        facade = AccessConfigFacade(await _service([_cfg("foo")]))
        result = await facade.get("foo")
        assert result is not None
        assert result.name == "foo"

    async def test_list_returns_all_entries(self) -> None:
        facade = AccessConfigFacade(await _service([_cfg("a"), _cfg("b")]))
        names = {c.name for c in await facade.list()}
        assert names == {"a", "b"}

    async def test_add_then_get_roundtrips(self) -> None:
        facade = AccessConfigFacade(await _service())
        await facade.add(_cfg("foo", gripper_offset=99.0), confirm=True)
        result = await facade.get("foo")
        assert result is not None
        assert result.gripper_offset == 99.0

    async def test_add_duplicate_propagates_store_error(self) -> None:
        facade = AccessConfigFacade(await _service([_cfg("dup")]))
        with pytest.raises(ValueError, match="already registered"):
            await facade.add(_cfg("dup"), confirm=True)

    async def test_update_replaces_value(self) -> None:
        facade = AccessConfigFacade(await _service([_cfg("foo")]))
        await facade.update(_cfg("foo", gripper_offset=42.0), confirm=True)
        result = await facade.get("foo")
        assert result is not None
        assert result.gripper_offset == 42.0

    async def test_update_unknown_propagates_keyerror(self) -> None:
        facade = AccessConfigFacade(await _service())
        with pytest.raises(KeyError, match="not found"):
            await facade.update(_cfg("ghost"), confirm=True)

    async def test_delete_returns_true_when_removed(self) -> None:
        facade = AccessConfigFacade(await _service([_cfg("foo")]))
        assert await facade.delete("foo", confirm=True) is True
        assert await facade.get("foo") is None

    async def test_delete_returns_false_when_absent(self) -> None:
        facade = AccessConfigFacade(await _service())
        assert await facade.delete("ghost", confirm=True) is False


class TestSafetyExceptionPropagation:
    """The facade does not enforce safety; it propagates whatever the store
    raises. This isolates REST/MCP from the impl detail of which store is
    behind the facade."""

    async def test_protected_error_propagates(self) -> None:

        class ProtectingStore:
            async def get(self, name: str) -> AccessConfig | None: return None
            async def list(self) -> List[AccessConfig]: return []
            async def add(self, config: AccessConfig) -> None: ...
            async def update(self, config: AccessConfig) -> None: ...
            async def delete(self, name: str) -> bool:
                raise ProtectedAccessConfigError(name)
            async def create_schema(self) -> None: ...
            async def aclose(self) -> None: ...

        facade = AccessConfigFacade(ProtectingStore())
        with pytest.raises(ProtectedAccessConfigError) as excinfo:
            await facade.delete("default_vertical", confirm=True)
        assert excinfo.value.name == "default_vertical"

    async def test_in_use_error_propagates(self) -> None:

        class InUseStore:
            async def get(self, name: str) -> AccessConfig | None: return None
            async def list(self) -> List[AccessConfig]: return []
            async def add(self, config: AccessConfig) -> None: ...
            async def update(self, config: AccessConfig) -> None: ...
            async def delete(self, name: str) -> bool:
                raise AccessConfigInUseError(name, referencing_count=3)
            async def create_schema(self) -> None: ...
            async def aclose(self) -> None: ...

        facade = AccessConfigFacade(InUseStore())
        with pytest.raises(AccessConfigInUseError) as excinfo:
            await facade.delete("nest_access", confirm=True)
        assert excinfo.value.referencing_count == 3
