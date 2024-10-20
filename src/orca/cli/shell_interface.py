
from abc import ABC
from typing import Optional, List, Dict, Any

from orca.driver_management.driver_installer import InstalledDriverInfo, DriverRegistryInfo


class IOrcaShell(ABC):
    def load(self, config_file: str):
        raise NotImplementedError

    def init(self, config_file: Optional[str] = None, resource_list: Optional[List[str]] = None, options: Dict[str, Any] = {}):
        raise NotImplementedError

    def run_workflow(self, workflow_name: str, config_file: Optional[str] = None, options: Dict[str, Any] = {}):
        raise NotImplementedError

    def run_method(self, method_name: str, start_map_json: str, end_map_json: str, config_file: Optional[str] = None, options: Dict[str, str] = {}):
        raise NotImplementedError
    
    def get_installed_drivers_info(self) -> Dict[str, InstalledDriverInfo]:
        raise NotImplementedError
    
    def get_available_drivers_info(self) -> Dict[str, DriverRegistryInfo]:
        raise NotImplementedError
    
    def install_driver(self, driver_name: str, driver_repo_url: Optional[str] = None):
        raise NotImplementedError
    
    def uninstall_driver(self, driver_name: str):
        raise NotImplementedError
    