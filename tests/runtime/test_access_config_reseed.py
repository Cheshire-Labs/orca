"""Access-config store survives multiple topology mounts (deployment-registries).

One deployment-wide access-config store outlives each mount: a second mount
re-declares its seed, and the factory reconciles insert-if-missing instead of
raising. Existing rows (operator edits) win over the re-declared seed.
"""

from cheshire_drivers.teachpoints import AccessConfig

from orca.runtime.access_config_service import AccessConfigService
from orca.runtime.db import create_memory_engine
from orca.runtime.sqlite_access_config_store import SqliteAccessConfigStore
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory


def _cfg(name: str, gripper_offset: float = 10.0) -> AccessConfig:
    return AccessConfig(
        name=name, access_type="vertical", gripper_offset=gripper_offset,
    )


def _service() -> AccessConfigService:
    return AccessConfigService(SqliteAccessConfigStore(create_memory_engine()))


async def test_seed_if_missing_inserts_only_new_names() -> None:
    service = _service()
    await service.add(_cfg("default_vertical", 10.0))
    await service.seed_if_missing([_cfg("default_vertical", 99.0), _cfg("extra", 5.0)])
    existing = await service.get("default_vertical")
    assert existing is not None
    assert existing.gripper_offset == 10.0  # operator value preserved
    assert await service.get("extra") is not None


async def test_factory_reconciles_seed_on_second_call() -> None:
    factory = InMemoryRuntimeStoreFactory()
    first = factory.access_configs(seed=[_cfg("default_vertical", 10.0)])
    # A second mount re-declares the same seed plus a new one: no raise.
    second = factory.access_configs(seed=[_cfg("default_vertical", 99.0), _cfg("h", 5.0)])
    assert second is first  # same deployment-wide singleton
    await factory.apply_seeds()
    keep = await second.get("default_vertical")
    assert keep is not None and keep.gripper_offset == 10.0
    assert await second.get("h") is not None


async def test_factory_access_configs_idempotent_with_no_seed() -> None:
    factory = InMemoryRuntimeStoreFactory()
    a = factory.access_configs()
    b = factory.access_configs()
    assert a is b
