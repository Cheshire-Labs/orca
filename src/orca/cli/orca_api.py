from types import MappingProxyType
from typing import Any, Dict, List, Literal, Union, Optional
import asyncio
import json
import logging
from orca.driver_management.driver_installer import  DriverInstaller, DriverLoader, DriverManager, DriverRegistryInfo, InstalledDriverInfo, InstalledDriverRegistry, RemoteAvailableDriverRegistry
from orca.orca_core import OrcaCore
from orca.system.method_executor import MethodTemplate
from orca.system.system import WorkflowTemplate
    
class OrcaApi:

    @property
    def _orca(self) -> OrcaCore:
        if self.__orca is None:
            raise ValueError("An orca configuration file has not been initialized.  Please load a configuration file.")
        return self.__orca
    
    def __init__(self) -> None:
        self.__orca: Optional[OrcaCore] = None
        
        available_drivers_registry: str = "https://raw.githubusercontent.com/Cheshire-Labs/orca-extensions/refs/heads/main/drivers.json"
        installed_registry = InstalledDriverRegistry("driver_manager/drivers.json")
        self._driver_manager = DriverManager(
            installed_registry,
            DriverLoader(), 
            DriverInstaller(installed_registry), 
            RemoteAvailableDriverRegistry(available_drivers_registry))

    def load(self, config_filepath: str):
        self.__orca = OrcaCore(config_filepath, self._driver_manager)

    def init(self,
            config_file: Optional[str] = None, 
            resource_list: Optional[List[str]] = None, 
            options: Dict[str, Any] = {}):
        if config_file is not None:
            self.load(config_file)

        asyncio.run(self._orca.initialize(resource_list, options))

    def run_workflow(self, 
                     workflow_name: str, 
                     config_file: Optional[str] = None, 
                     options: Dict[str, Any] = {}):
        if config_file is not None:
            self.load(config_file)
        asyncio.run(self._orca.run_workflow(workflow_name))

    def run_method(self,
                   method_name: str, 
                   start_map_json: str, 
                   end_map_json: str,
                   config_file: Optional[str] = None, 
                   options: Dict[str, str] = {}):
        if config_file is not None:
            self.load(config_file)
        start_map = json.loads(start_map_json)
        end_map = json.loads(end_map_json)
        asyncio.run(self._orca.run_method(method_name, start_map, end_map))

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
    
    def install_driver(self, 
                       driver_name: str, 
                       driver_repo_url: Optional[str] = None) -> None:
        self._driver_manager.install_driver(driver_name, driver_repo_url)

    
    def uninstall_driver(self, driver_name: str) -> None:
        self._driver_manager.uninstall_driver(driver_name)

    def set_logging_destination(self, destination: Optional[Union[str, logging.Handler]] = None, logging_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO") -> None:
        OrcaCore.set_logging_destination(destination, logging_level)
    