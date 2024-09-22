from abc import ABC
from dataclasses import dataclass
import json
import os
import shutil
from typing import Any, Dict, Optional
import requests
from zipfile import ZipFile
from importlib import import_module


from orca_driver_interface.driver_interfaces import IDriver

@dataclass
class DriverInfo:
    name: str
    version: str
    description: str
    repository: str


class AvailableDriverRegistry(ABC):
    def __init__(self, registry: Dict[str, Any]) -> None:
        self._registry = registry

    def get_driver_info(self, driver_name: str) -> DriverInfo:
        for driver in self._registry['drivers']:
            if driver['name'] == driver_name:
                return driver
        raise Exception(f"Driver '{driver_name}' not found in the registry")


class LocalAvailableDriverRegistry(AvailableDriverRegistry):
    def __init__(self, registry_json_filepath: str) -> None:
        self._registry_filepath = registry_json_filepath
        self._registry = self._load_registry()
        super().__init__(self._registry)

    def _load_registry(self) -> Dict[str, Any]:
        if os.path.exists(self._registry_filepath):
            with open(self._registry_filepath, 'r') as file:
                return json.load(file)
        else:
            raise Exception(f"Registry file '{self._registry_filepath}' not found")

class RemoteAvailableDriverRegistry(AvailableDriverRegistry):
    def __init__(self, registry_json_url: str) -> None:
        self._registry_url = registry_json_url
        self._registry = self._load_registry()
        super().__init__(self._registry)

    def _load_registry(self) -> Dict[str, Any]:
        response = requests.get(self._registry_url )
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to load driver registry from {self._registry_url }")


class DriverInstaller:
    '''
    Responsible for installing and uninstalling drivers.
    '''
    def __init__(self, drivers_dir: str) -> None:
        self._drivers_dir = drivers_dir
        os.makedirs(self._drivers_dir, exist_ok=True)

    def download_and_extract_driver(self, driver_name: str, driver_repo_url: str, branch: str = 'main') -> str:
        zip_url = f"{driver_repo_url}/archive/refs/heads/{branch}.zip"
        zip_path = os.path.join(self._drivers_dir, driver_name + '.zip')

        # Download the zip file
        with requests.get(zip_url, stream=True) as r:
            if r.status_code != 200:
                raise Exception(f"Failed to download the driver from {zip_url}")
            with open(zip_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)

        # Extract the zip file
        with ZipFile(zip_path, 'r') as zip_ref:
            extract_path = os.path.join(self._drivers_dir, driver_name)
            zip_ref.extractall(extract_path)

        # Remove the zip file
        os.remove(zip_path)
        return extract_path

    def uninstall_driver(self, driver_name: str) -> None:
        driver_path = os.path.join(self._drivers_dir, driver_name)
        if os.path.exists(driver_path):
            shutil.rmtree(driver_path)

class InstalledDriverRegistry:
    def __init__(self, installed_json: str) -> None:
        self._installed_file = installed_json
        self._installed_drivers = self._load_installed_drivers()

    def _load_installed_drivers(self) -> Dict[str, DriverInfo]:
        if os.path.exists(self._installed_file):
            with open(self._installed_file, 'r') as file:
                return json.load(file)
        else:
            return {}

    def save_installed_drivers(self) -> None:
        with open(self._installed_file, 'w') as file:
            json.dump(self._installed_drivers, file, indent=2)

    def is_driver_installed(self, driver_name: str) -> bool:
        return driver_name in self._installed_drivers

    def add_driver(self, driver_name: str, driver_info: DriverInfo) -> None:
        self._installed_drivers[driver_name] = driver_info
        self.save_installed_drivers()

    def remove_driver(self, driver_name: str) -> None:
        if driver_name in self._installed_drivers:
            del self._installed_drivers[driver_name]
            self.save_installed_drivers()





class DriverLoader:

    def __init__(self) -> None:
        self._drivers_dir = os.path.join(os.path.dirname(__file__), "drivers")  

    def load_driver(self, driver_name: str) -> IDriver:
        try:
            # Construct the directory path for the driver
            driver_dir = os.path.join(self._drivers_dir, driver_name)
            
            # Check if the driver directory exists
            if not os.path.isdir(driver_dir):
                raise FileNotFoundError(f"Driver directory '{driver_dir}' not found. Ensure the directory exists and follows the naming convention.")

            # Construct the module path for the driver
            module_name = f"orca.driver_management.drivers.{driver_name}.{driver_name}"
            
            # Check if the driver module file exists
            module_file_path = os.path.join(driver_dir, f"{driver_name}.py")
            if not os.path.isfile(module_file_path):
                raise FileNotFoundError(f"Driver module file '{module_file_path}' not found. Ensure the file exists and follows the naming convention.")

            # Import the driver module
            driver_module = import_module(module_name)
            
            # Convert driver_name to CamelCase and append 'Driver' for the class name
            class_name = ''.join(part.capitalize() for part in driver_name.split('_')) + 'Driver'
            
            # Check if the class exists in the module
            if not hasattr(driver_module, class_name):
                raise AttributeError(f"Class '{class_name}' not found in module '{module_name}'. Ensure the class is correctly named and defined in the module.")
            
            driver_class = getattr(driver_module, class_name)
            
            # Ensure the class implements the IDriver interface
            if not issubclass(driver_class, IDriver):
                raise TypeError(f"Class '{class_name}' in module '{module_name}' does not implement IDriver.")
            
            driver = driver_class()

            return driver
        
        except (FileNotFoundError, ImportError, AttributeError, TypeError) as e:
            print(f"Error loading driver '{driver_name}': {e}")
            raise   
    

class IDriverManager(ABC):
    def get_driver(self, driver_name: str) -> IDriver:
        raise NotImplementedError
    
    def install_driver(self, driver_name: str, driver_repo_url: Optional[str] = None, driver_repo_branch: Optional[str] = None) -> None:
        raise NotImplementedError

class DriverManager(IDriverManager):
    def __init__(self, 
                 installed_registry: InstalledDriverRegistry, 
                 driver_loader: DriverLoader, 
                 driver_installer: DriverInstaller,
                 driver_registry: AvailableDriverRegistry):
        self._installed_registry = installed_registry
        self._driver_loader = driver_loader
        self._driver_installer = driver_installer
        self._driver_registry = driver_registry

    def get_driver(self, driver_name: str) -> IDriver:
        if self._installed_registry.is_driver_installed(driver_name):
            return self._driver_loader.load_driver(driver_name)
        else:
            raise KeyError(f"Driver '{driver_name}' is not installed")


    def install_driver(self, driver_name: str, driver_repo_url: Optional[str] = None, driver_repo_branch: Optional[str] = None) -> None:
        if driver_repo_url is None:
            driver_info = self._driver_registry.get_driver_info(driver_name)
            driver_repo_url = driver_info.repository
        
        if driver_repo_branch is not None:
            self._driver_installer.download_and_extract_driver(driver_name, driver_repo_url, driver_repo_branch)
        else:
            self._driver_installer.download_and_extract_driver(driver_name, driver_repo_url)

    def uninstall_driver(self, driver_name: str) -> None:
        self._driver_installer.uninstall_driver(driver_name)