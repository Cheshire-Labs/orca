"""DeploymentRegistries exposes the config facades over the given stores."""

from orca.runtime.access_config_service import AccessConfigService
from orca.runtime.db import create_memory_engine
from orca.runtime.deployment_registries import DeploymentRegistries
from orca.runtime.facades.access_configs import AccessConfigFacade
from orca.runtime.facades.move_defaults import MoveDefaultsFacade
from orca.runtime.facades.profiles import DeploymentProfileFacade
from orca.runtime.move_defaults_service import MoveDefaultsService
from orca.runtime.sqlite_move_defaults_store import SqliteMoveDefaultsStore
from orca.runtime.labware_catalog_service import (
    LabwareCatalogService,
    seeded_labware_catalog_service,
)
from orca.runtime.profile_store import FileDeploymentProfileStore
from orca.runtime.sqlite_access_config_store import SqliteAccessConfigStore
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.sqlite_grip_profile_store import SqliteGripProfileStore


def _registries(tmp_path) -> DeploymentRegistries:
    return DeploymentRegistries(
        labware_catalog=seeded_labware_catalog_service(),
        access_config_store=AccessConfigService(
            SqliteAccessConfigStore(create_memory_engine())
        ),
        move_defaults_service=MoveDefaultsService(
            SqliteMoveDefaultsStore(create_memory_engine())
        ),
        grip_profile_service=GripProfileService(
            SqliteGripProfileStore(create_memory_engine())
        ),
        profile_store=FileDeploymentProfileStore(tmp_path),
    )


def test_exposes_every_config_facade(tmp_path) -> None:
    registries = _registries(tmp_path)
    assert isinstance(registries.labware_catalog, LabwareCatalogService)
    assert isinstance(registries.access_configs, AccessConfigFacade)
    assert isinstance(registries.move_defaults, MoveDefaultsFacade)
    assert isinstance(registries.profiles, DeploymentProfileFacade)


async def test_catalog_facade_reads_the_seed(tmp_path) -> None:
    registries = _registries(tmp_path)
    rows = await registries.labware_catalog.list(None)
    assert len(rows) > 0


async def test_facades_are_stable_across_access(tmp_path) -> None:
    registries = _registries(tmp_path)
    assert registries.access_configs is registries.access_configs
    assert registries.move_defaults is registries.move_defaults
    assert registries.profiles is registries.profiles
