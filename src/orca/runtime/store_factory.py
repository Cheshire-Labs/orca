"""The setup stores of one deployment.

Setup is files in the deployment package. The build attaches each device's
teachpoints and deck layouts from this factory; a topology never asks for them.
Each server builds one `orca.runtime.startup.PackageSetupStoreFactory` over its package folder.
"""

import os
from pathlib import Path
from typing import Protocol

from orca.config import OrcaConfig
from orca.runtime.access_config_service import IAccessConfigService
from orca.runtime.deck_layout_service import IDeckLayoutService
from orca.runtime.deployment_profile_service import IDeploymentProfileService
from orca.runtime.device_setup_service import IDeviceSetupService
from orca.runtime.grip_profile_service import IGripProfileService
from orca.resource_models.labware_catalog_protocol import ILabwareCatalog
from orca.runtime.labware_catalog_service import ILabwareCatalogService
from orca.runtime.move_defaults_service import IMoveDefaultsService
from orca.runtime.teachpoint_service import ITeachpointService
from orca.runtime.global_variable_service import IGlobalVariableService
from orca.variables.variable_store import IVariableService


class IRuntimeStoreFactory(Protocol):
    """The setup stores of one deployment. `teachpoints` and `deck_layouts` are per device."""

    def teachpoints(self, device_id: str) -> ITeachpointService: ...

    def deck_layouts(self, device_id: str) -> IDeckLayoutService: ...

    def device_setups(self) -> IDeviceSetupService: ...

    def access_configs(self) -> IAccessConfigService: ...

    def move_defaults(self) -> IMoveDefaultsService: ...

    def grip_profiles(self) -> IGripProfileService: ...

    def profiles(self) -> IDeploymentProfileService: ...

    def system_config(self) -> OrcaConfig:
        """The deployment's settings: ``system_config.json``, or the defaults when there is none."""
        ...

    def global_variables(self) -> IGlobalVariableService:
        """``global_variables.json``: the globals it declares and the values an operator set."""
        ...

    def variable_store(self) -> IVariableService:
        """A fresh variable store for one system build. Variables are runtime state, not setup."""
        ...

    def labware_catalog_store(self) -> ILabwareCatalogService:
        """The labware catalog: built-ins from cheshire-drivers plus ``labware/<name>.json``."""
        ...

    def labware_catalog(self) -> ILabwareCatalog:
        """A read view over ``labware_catalog_store``, so an edit resolves on the next read."""
        ...


def default_package_root() -> Path:
    """The deployment package folder a daemon keeps its setup files in:
    ``ORCA_PACKAGE_DIR``, or the folder it was started in."""
    return Path(os.environ.get("ORCA_PACKAGE_DIR") or Path.cwd())
