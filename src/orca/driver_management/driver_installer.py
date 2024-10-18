from abc import ABC
import importlib
import subprocess
from dataclasses import dataclass
import json
import os
import sys
from typing import Any, Dict, Optional
import requests


from orca_driver_interface.driver_interfaces import IDriver

@dataclass
class DriverInfo:
    name: str
    description: str
    url: str


class AvailableDriverRegistry(ABC):
    def __init__(self, registry: Dict[str, Any]) -> None:
        self._registry = registry

    def get_driver_info(self, driver_name: str) -> DriverInfo:
        for driver in self._registry['drivers']:
            if driver['name'] == driver_name:
                return  DriverInfo(**driver)
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
    Responsible for installing and uninstalling drivers using pip.
    '''

    def install_driver(self, driver_name: str, driver_repo_url: str) -> None:
        '''
        Installs the driver using pip. 
        If a driver_repo_url is provided, it installs directly from that repository.
        Optionally, a specific version can be installed.
        '''
        try:
            # Install from the given repository URL (e.g., GitHub)
            install_command = [sys.executable, "-m", "pip", "install", f"git+{driver_repo_url}"]

            # Run the pip install command
            result = subprocess.check_call(install_command)
            print(f"Driver '{driver_name}' installed successfully.")

        except subprocess.CalledProcessError as e:
            print(f"Error occurred during installation of driver '{driver_name}': {e}")
            print(f"Error Output: {e.stderr}")

    def uninstall_driver(self, driver_name: str) -> None:
        '''
        Uninstalls the driver using pip.
        '''
        try:
            uninstall_command = [sys.executable, "-m", "pip", "uninstall", "-y", driver_name]
            subprocess.check_call(uninstall_command)
            print(f"Driver '{driver_name}' uninstalled successfully.")
        
        except subprocess.CalledProcessError as e:
            print(f"Failed to uninstall driver '{driver_name}': {e}")
            raise


class DriverLoader:
    '''
    Loads drivers that are installed via pip.
    '''
    def load_driver(self, driver_name: str) -> IDriver:
        try:
            # Assume the driver module is installed and available in the environment
            # The driver name corresponds to the installed package/module name
            module_name = f"{driver_name}.driver"  # Assuming all drivers have a `driver.py` entry point

            # Import the driver module
            driver_module = importlib.import_module(module_name)
            
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
        
        except (ImportError, AttributeError, TypeError) as e:
            print(f"Error loading driver '{driver_name}': {e}")
            raise


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

class IDriverManager(ABC):
    def get_driver(self, driver_name: str) -> IDriver:
        raise NotImplementedError
    
    def install_driver(self, driver_name: str, driver_repo_url: Optional[str]) -> None:
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


    def install_driver(self, driver_name: str, driver_repo_url: Optional[str]) -> None:
        if driver_repo_url is None:
            driver_info = self._driver_registry.get_driver_info(driver_name)
            driver_repo_url = driver_info.url
        
        self._driver_installer.install_driver(driver_name, driver_repo_url)

    def uninstall_driver(self, driver_name: str) -> None:
        self._driver_installer.uninstall_driver(driver_name)