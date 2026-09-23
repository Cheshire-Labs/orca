"""Deployment-scoped config registries that live in front of the runtime.

The labware catalog, access configs, move defaults, and deployment profiles are
deployment config, not execution state: an operator authors systems against them with no
devices connected and no system deployed. They must therefore be reachable
before (and independent of) any built ``SystemRuntime``.

``IDeploymentRegistries`` is that always-available holder. It owns the operator
CRUD facades; storage stays behind the ``I*Store`` interfaces so the
source-available default (in-memory / file) and a hosted deployment (DB-backed) differ only
in which store instances are passed in. The runtime, when present, keeps the SAME stores for
build-time and submit-time resolution -- single source of truth, never raw DB.
"""

from typing import Protocol

from orca.runtime.facades.access_configs import AccessConfigFacade
from orca.runtime.facades.labware_catalog import ILabwareCatalogFacade
from orca.runtime.facades.grip_profiles import GripProfileFacade
from orca.runtime.facades.move_defaults import MoveDefaultsFacade
from orca.runtime.facades.profiles import DeploymentProfileFacade
from orca.runtime.interfaces import (
    IAccessConfigStore,
    IDeploymentProfileStore,
)
from orca.runtime.move_defaults_service import MoveDefaultsService
from orca.runtime.grip_profile_service import GripProfileService
from orca.runtime.labware_catalog_service import LabwareCatalogService
from orca.runtime.runtime_interface import (
    IAccessConfigFacade,
    IDeploymentProfileFacade,
    IGripProfileFacade,
    IMoveDefaultsFacade,
)
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory


class IDeploymentRegistries(Protocol):
    """Always-available holder for the deployment-scoped config facades."""

    @property
    def labware_catalog(self) -> ILabwareCatalogFacade: ...
    @property
    def access_configs(self) -> IAccessConfigFacade: ...
    @property
    def move_defaults(self) -> IMoveDefaultsFacade: ...
    @property
    def grip_profiles(self) -> IGripProfileFacade: ...
    @property
    def profiles(self) -> IDeploymentProfileFacade: ...


class DeploymentRegistries:
    """Concrete registries built over the deployment's config stores.

    Backend-agnostic: the daemon passes in-memory / file stores, a hosted
    deployment passes DB-backed stores. The facades are the same; only the substrate moves.
    """

    def __init__(
        self,
        *,
        labware_catalog: LabwareCatalogService,
        access_config_store: IAccessConfigStore,
        move_defaults_service: MoveDefaultsService,
        grip_profile_service: GripProfileService,
        profile_store: IDeploymentProfileStore,
    ) -> None:
        self._labware_catalog: ILabwareCatalogFacade = labware_catalog
        self._access_configs: IAccessConfigFacade = AccessConfigFacade(
            access_config_store,
        )
        self._move_defaults: IMoveDefaultsFacade = MoveDefaultsFacade(
            move_defaults_service,
        )
        self._grip_profiles: IGripProfileFacade = GripProfileFacade(
            grip_profile_service,
        )
        self._profiles: IDeploymentProfileFacade = DeploymentProfileFacade(
            profile_store,
        )

    @property
    def labware_catalog(self) -> ILabwareCatalogFacade:
        return self._labware_catalog

    @property
    def access_configs(self) -> IAccessConfigFacade:
        return self._access_configs

    @property
    def move_defaults(self) -> IMoveDefaultsFacade:
        return self._move_defaults

    @property
    def grip_profiles(self) -> IGripProfileFacade:
        return self._grip_profiles

    @property
    def profiles(self) -> IDeploymentProfileFacade:
        return self._profiles


def build_in_memory_deployment_layer() -> (
    tuple["InMemoryRuntimeStoreFactory", DeploymentRegistries]
):
    """One in-memory store factory + the deployment-registries layer over it.

    Shared by daemon startup (fresh daemon) and the mount-topology fallback so
    the factory and registries are wired identically and never diverge.
    """
    from orca.runtime.store_factory import InMemoryRuntimeStoreFactory

    factory = InMemoryRuntimeStoreFactory()
    registries = DeploymentRegistries(
        labware_catalog=factory.labware_catalog_store(),
        access_config_store=factory.access_configs(),
        move_defaults_service=factory.move_defaults(),
        grip_profile_service=factory.grip_profiles(),
        profile_store=factory.profiles(),
    )
    return factory, registries
