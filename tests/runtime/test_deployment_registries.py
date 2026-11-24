"""DeploymentRegistries exposes the config facades over the given stores."""

from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.facades.access_configs import AccessConfigFacade
from orca.runtime.facades.move_defaults import MoveDefaultsFacade
from orca.runtime.facades.profiles import DeploymentProfileFacade
from orca.runtime.labware_catalog_service import LabwareCatalogService
from orca.runtime.startup import deployment_registries_over
from tests.setup_package import package_stores


def _registries(tmp_path) -> IDeploymentRegistries:
    return deployment_registries_over(package_stores(tmp_path))


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
