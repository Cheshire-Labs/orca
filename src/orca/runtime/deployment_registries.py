"""Deployment-scoped config registries that live in front of the runtime.

The labware catalog, access configs, move defaults, deployment profiles and device setups are
deployment config, not execution state: an operator authors systems against them with no
devices connected and no system deployed. They must therefore be reachable
before (and independent of) any built ``SystemRuntime``.

``IDeploymentRegistries`` is that always-available holder of the operator CRUD
facades over the setup stores. Startup builds it over the same stores the
runtime reads (`orca.runtime.startup.deployment_registries_over`).
"""

from dataclasses import dataclass
from typing import Protocol

from orca.runtime.facades.device_setups import IDeviceSetupFacade
from orca.runtime.facades.labware_catalog import ILabwareCatalogFacade
from orca.runtime.facade_interfaces import (
    IAccessConfigFacade,
    IDeploymentProfileFacade,
    IGripProfileFacade,
    IMoveDefaultsFacade,
)


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
    @property
    def device_setups(self) -> IDeviceSetupFacade: ...


@dataclass(frozen=True)
class DeploymentRegistries:
    labware_catalog: ILabwareCatalogFacade
    access_configs: IAccessConfigFacade
    move_defaults: IMoveDefaultsFacade
    grip_profiles: IGripProfileFacade
    profiles: IDeploymentProfileFacade
    device_setups: IDeviceSetupFacade
