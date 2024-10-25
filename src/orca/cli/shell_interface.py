
from abc import ABC, abstractmethod
import logging
from typing import Optional, List, Dict, Union, Literal, Any

from orca.driver_management.driver_installer import InstalledDriverInfo, DriverRegistryInfo


class IOrcaShell(ABC):

    @abstractmethod
    def load(self, config_file: str):
        raise NotImplementedError

    @abstractmethod
    def init(self, config_file: Optional[str] = None, resource_list: Optional[List[str]] = None, options: Dict[str, Any] = {}):
        raise NotImplementedError

    @abstractmethod
    def run_workflow(self, workflow_name: str, config_file: Optional[str] = None, options: Dict[str, Any] = {}):
        raise NotImplementedError

    @abstractmethod
    def run_method(self, method_name: str, start_map_json: str, end_map_json: str, config_file: Optional[str] = None, options: Dict[str, str] = {}):
        raise NotImplementedError
    
    @abstractmethod
    def get_workflow_recipes(self) -> List[str]:
        raise NotImplementedError
    
    @abstractmethod
    def get_method_recipes(self) -> List[str]:
        raise NotImplementedError
    
    @abstractmethod
    def get_labware_recipes(self) -> List[str]:
        raise NotImplementedError
    
    @abstractmethod
    def get_running_workflows(self) -> Dict[str, str]:
        raise NotImplementedError
    
    @abstractmethod
    def get_running_methods(self) -> Dict[str, str]:
        raise NotImplementedError
    
    @abstractmethod
    def get_locations(self) -> List[str]:
        raise NotImplementedError
    
    @abstractmethod
    def get_resources(self) -> List[str]:
        raise NotImplementedError

    @abstractmethod
    def get_equipments(self) -> List[str]:  
        raise NotImplementedError
    
    @abstractmethod
    def get_transporters(self) -> List[str]:
        raise NotImplementedError
    
    @abstractmethod
    def get_installed_drivers_info(self) -> Dict[str, InstalledDriverInfo]:
        raise NotImplementedError
    
    @abstractmethod
    def get_available_drivers_info(self) -> Dict[str, DriverRegistryInfo]:
        raise NotImplementedError
    
    @abstractmethod
    def install_driver(self, driver_name: str, driver_repo_url: Optional[str] = None):
        raise NotImplementedError
    
    @abstractmethod
    def uninstall_driver(self, driver_name: str):
        raise NotImplementedError
    
    @abstractmethod
    def set_logging_destination(self, destination: Optional[Union[str, logging.Handler]] = None, logging_level: Literal['debug', 'info', 'warning', 'error'] = 'info') -> None:
        raise NotImplementedError
    