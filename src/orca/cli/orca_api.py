import logging
from types import MappingProxyType
from typing import Dict, List, Optional
import asyncio
from orca.driver_management.driver_installer import \
    DriverInstaller, DriverLoader, DriverManager, DriverRegistryInfo, \
    InstalledDriverInfo, InstalledDriverRegistry, RemoteAvailableDriverRegistry
from orca.orca_core import OrcaCore
from orca.workflow_models.method_template import MethodTemplate
from orca.system.system import WorkflowTemplate

orca_logger = logging.getLogger("orca")

class OrcaApi:

    @property
    def _orca(self) -> OrcaCore:
        if self.__orca is None:
            raise ValueError(
                "An orca configuration file has not been initialized.  Please load a configuration file."
            )
        return self.__orca

    def __init__(
        self,
        available_drivers_registry: str = "https://raw.githubusercontent.com/Cheshire-Labs/orca-extensions/refs/heads/main/drivers.json",
    ) -> None:
        try:
            self.__orca: Optional[OrcaCore] = None
            installed_registry = InstalledDriverRegistry()
            self._driver_manager = DriverManager(
                installed_registry,
                DriverLoader(), 
                DriverInstaller(installed_registry), 
                RemoteAvailableDriverRegistry(available_drivers_registry, installed_registry))
        except Exception as e:
            orca_logger.error(f"Failed to initialize OrcaApi: {e}")
            raise

    def load(self, config_filepath: str) -> None:
        try:
            self.__orca = OrcaCore(config_filepath, self._driver_manager)
        except Exception as e:
            orca_logger.error(f"Failed to load configuration file: {e}")
            raise

    def init(
        self,
        config_file: Optional[str] = None,
        resource_list: Optional[List[str]] = None,
        deployment_stage: Optional[str] = None,
    ) -> None:
        if config_file is not None:
            self.load(config_file)

        asyncio.run(self._orca.initialize(resource_list, deployment_stage))

    def get_deployment_stages(self) -> List[str]:
        config_file_model = self._orca.system_config
        variable_configs = config_file_model.config
        return list(variable_configs.model_extra.keys())

    def run_workflow(self,
                     workflow_name: str,
                     config_file: Optional[str] = None,
                     deployment_stage: Optional[str] = None,
                     ) -> str:
        if config_file is not None:
            self.load(config_file)
        instance_id = self._orca.create_workflow_instance(workflow_name, deployment_stage)
        loop = asyncio.get_running_loop()
        loop.create_task(self._orca.run_workflow(instance_id))
        return instance_id

    def run_method(
        self,
        method_name: str,
        start_map: Dict[str, str],
        end_map: Dict[str, str],
        config_file: Optional[str] = None,
        deployment_stage: Optional[str] = None,
                    ) -> str:

        if config_file is not None:
            self.load(config_file)
        method_id = self._orca.create_method_instance(method_name, start_map, end_map, deployment_stage)
        loop = asyncio.get_running_loop()
        loop.create_task(self._orca.run_method(method_id))
        return method_id

    def get_workflow_recipes(self) -> MappingProxyType[str, WorkflowTemplate]:
        return self._orca.system.get_workflow_templates()

    def get_method_recipes(self) -> MappingProxyType[str, MethodTemplate]:
        return self._orca.system.get_method_templates()

    def get_labware_recipes(self) -> List[str]:
        raise NotImplementedError

    def get_running_workflows(self) -> Dict[str, str]:
        raise NotImplementedError

    def get_running_methods(self) -> Dict[str, str]:
        raise NotImplementedError

    def get_locations(self) -> List[str]:
        return [location.name for location in self._orca.system.locations]

    def get_resources(self) -> List[str]:
        return [resource.name for resource in self._orca.system.resources]

    def get_equipments(self) -> List[str]:
        return [r.name for r in self._orca.system.equipments]

    def get_transporters(self) -> List[str]:
        return [r.name for r in self._orca.system.transporters]

    def stop(self) -> None:
        self._orca.stop()

    def get_installed_drivers_info(self) -> Dict[str, InstalledDriverInfo]:
        return self._driver_manager.get_installed_drivers_info()

    def get_available_drivers_info(self) -> Dict[str, DriverRegistryInfo]:
        return self._driver_manager.get_available_drivers_info()

    def install_driver(
        self, driver_name: str, driver_repo_url: Optional[str] = None
    ) -> None:
        self._driver_manager.install_driver(driver_name, driver_repo_url)

    def uninstall_driver(self, driver_name: str) -> None:
        self._driver_manager.uninstall_driver(driver_name)
