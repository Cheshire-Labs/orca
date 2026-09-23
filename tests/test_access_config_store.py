"""Tests for the SQLite access-config store and the AccessConfigService.

The store is the dumb per-DB persistence behind IAccessConfigStore; the Service
is the on-loop orchestration (single-lock CRUD) the runtime owns. Both honor the
same CRUD contract: add raises on duplicate, update raises on unknown, delete
returns whether a row was removed.
"""

import pytest
from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.access_config_service import AccessConfigService
from orca.runtime.db import create_memory_engine
from orca.runtime.sqlite_access_config_store import SqliteAccessConfigStore


def _vertical(name: str = "v") -> AccessConfig:
    return AccessConfig(
        name=name,
        access_type="vertical",
        gripper_offset=20.0,
        vertical_clearance=20.0,
        horizontal_clearance=100.0,
    )


def _horizontal(name: str = "h") -> AccessConfig:
    return AccessConfig(
        name=name,
        access_type="horizontal",
        gripper_offset=25.0,
        vertical_clearance=30.0,
        horizontal_clearance=120.0,
    )


def _store() -> SqliteAccessConfigStore:
    return SqliteAccessConfigStore(create_memory_engine())


def _service() -> AccessConfigService:
    return AccessConfigService(SqliteAccessConfigStore(create_memory_engine()))


class TestSqliteAccessConfigStore:

    async def test_get_returns_none_for_unknown(self) -> None:
        assert await _store().get("missing") is None

    async def test_add_then_get_roundtrips(self) -> None:
        store = _store()
        await store.add(_vertical("v1"))
        loaded = await store.get("v1")
        assert loaded is not None
        assert loaded.name == "v1"
        assert loaded.access_type == "vertical"
        assert loaded.gripper_offset == 20.0

    async def test_list_returns_all(self) -> None:
        store = _store()
        await store.add(_vertical("v"))
        await store.add(_horizontal("h"))
        result = await store.list()
        assert {c.name for c in result} == {"v", "h"}

    async def test_add_duplicate_name_raises(self) -> None:
        store = _store()
        await store.add(_vertical("dup"))
        with pytest.raises(ValueError, match="already registered"):
            await store.add(_vertical("dup"))

    async def test_update_replaces_value(self) -> None:
        store = _store()
        await store.add(_vertical("v"))
        replacement = AccessConfig(
            name="v",
            access_type="vertical",
            gripper_offset=99.0,
            vertical_clearance=20.0,
            horizontal_clearance=100.0,
        )
        await store.update(replacement)
        loaded = await store.get("v")
        assert loaded is not None
        assert loaded.gripper_offset == 99.0

    async def test_update_unknown_raises(self) -> None:
        store = _store()
        with pytest.raises(KeyError, match="not found"):
            await store.update(_vertical("ghost"))

    async def test_delete_returns_true_when_present(self) -> None:
        store = _store()
        await store.add(_vertical("v"))
        assert await store.delete("v") is True
        assert await store.get("v") is None

    async def test_delete_returns_false_when_absent(self) -> None:
        assert await _store().delete("ghost") is False


class TestAccessConfigService:
    """The Service delegates CRUD to the store under one lock, same contract."""

    async def test_add_then_get_roundtrips(self) -> None:
        service = _service()
        await service.add(_vertical("v1"))
        loaded = await service.get("v1")
        assert loaded is not None and loaded.name == "v1"

    async def test_add_duplicate_name_raises(self) -> None:
        service = _service()
        await service.add(_vertical("dup"))
        with pytest.raises(ValueError, match="already registered"):
            await service.add(_vertical("dup"))

    async def test_update_unknown_raises(self) -> None:
        service = _service()
        with pytest.raises(KeyError, match="not found"):
            await service.update(_vertical("ghost"))

    async def test_delete_returns_true_when_present(self) -> None:
        service = _service()
        await service.add(_vertical("v"))
        assert await service.delete("v") is True
        assert await service.get("v") is None

    async def test_seed_if_missing_preserves_existing(self) -> None:
        service = _service()
        await service.add(_vertical("keep"))
        await service.seed_if_missing([_horizontal("keep"), _vertical("new")])
        keep = await service.get("keep")
        assert keep is not None and keep.access_type == "vertical"  # not overwritten
        assert await service.get("new") is not None
