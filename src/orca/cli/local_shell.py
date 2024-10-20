import os
from typing import Any, Dict, List, Optional
import asyncio
import json
from orca.driver_management.driver_installer import  DriverInstaller, DriverLoader, DriverManager, DriverRegistryInfo, InstalledDriverInfo, InstalledDriverRegistry, RemoteAvailableDriverRegistry
from orca.orca_core import OrcaCore, PROJECT_ROOT

from orca.cli.shell_interface import IOrcaShell
    
class LocalOrcaShell(IOrcaShell):
    def __init__(self) -> None:
        self._orca: Optional[OrcaCore] = None
        
        available_drivers_registry: str = "https://raw.githubusercontent.com/Cheshire-Labs/orca-extensions/refs/heads/main/drivers.json"
        installed_registry = InstalledDriverRegistry("driver_manager/drivers.json")
        self._driver_manager = DriverManager(
            installed_registry,
            DriverLoader(), 
            DriverInstaller(installed_registry), 
            RemoteAvailableDriverRegistry(available_drivers_registry))

    def load(self, config_filepath: str):
        self._orca = OrcaCore(config_filepath, self._driver_manager)

    def init(self,
            config_file: Optional[str] = None, 
            resource_list: Optional[List[str]] = None, 
            options: Dict[str, Any] = {}):
        if config_file is not None:
            self.load(config_file)
        if self._orca is None:
            raise ValueError("An orca configuration file has not been initialized.  Please provide a configuration file.")
        asyncio.run(self._orca.initialize(resource_list, options))

    def run_workflow(self, 
                     workflow_name: str, 
                     config_file: Optional[str] = None, 
                     options: Dict[str, Any] = {}):
        if config_file is not None:
            self.load(config_file)
        if self._orca is None:
            raise ValueError("An orca configuration file has not been initialized.  Please provide a configuration file.")
        asyncio.run(self._orca.run_workflow(workflow_name))

    def run_method(self,
                   method_name: str, 
                   start_map_json: str, 
                   end_map_json: str,
                   config_file: Optional[str] = None, 
                   options: Dict[str, str] = {}):
        if config_file is not None:
            self.load(config_file)
        if self._orca is None:
            raise ValueError("An orca configuration file has not been initialized.  Please provide a configuration file.")
        start_map = json.loads(start_map_json)
        end_map = json.loads(end_map_json)
        asyncio.run(self._orca.run_method(method_name, start_map, end_map))
    
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

    