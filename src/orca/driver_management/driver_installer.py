"""
Driver management module.
"""
from abc import ABC
import importlib
import importlib.metadata
import importlib.resources
import subprocess
from pydantic import BaseModel, Field
import json
import os
import sys
from typing import Any, Dict, List, Optional
import requests

from orca_driver_interface.driver_interfaces import IDriver


class CommandParamSchema(BaseModel):
    name: str
    type: str = 'string'
    description: str = ''
    required: bool = False


class CommandSchema(BaseModel):
    name: str
    description: str = ''
    params: List[CommandParamSchema] = Field(default_factory=list)


class DriverInfo(BaseModel):
    name: str
    driverPath: str
    driverClass: str
    type: str = 'labwareable'
    description: str = ''
    initParams: List[CommandParamSchema] = Field(default_factory=list)
    commands: List[CommandSchema] = Field(default_factory=list)


class InstalledDriverInfo(DriverInfo):
    packageName: str


class DriverRegistryInfo(BaseModel):
    name: str
    description: str
    url: str


class InstalledDriverRegistry:
    @property
    def installed_drivers(self) -> Dict[str, InstalledDriverInfo]:
        return self._installed_drivers

    def __init__(self) -> None:
        self._prefix = "orca-driver-"
        self.refresh()

    def get_installed_driver_info(self, driver_name: str) -> InstalledDriverInfo:
        return self.installed_drivers[driver_name]

    def is_driver_installed(self, driver_name: str) -> bool:
        return driver_name in self._installed_drivers

    def refresh(self) -> None:
        self._installed_drivers: Dict[str, InstalledDriverInfo] = {}
        self._discover_installed_drivers()

    def _discover_installed_drivers(self) -> None:
        for dist in importlib.metadata.distributions():
            name = dist.metadata["Name"]
            if name is None:
                continue
            if name.startswith(self._prefix):
                driver_name = name.replace(self._prefix, "")
                # get the driver info file
                driver_info_path = importlib.resources.files(f"{driver_name}_driver").joinpath("driver.json")
                driver_info_text = driver_info_path.read_text()
                driver_info: Dict[str, Any] = json.loads(driver_info_text)

                installed_driver_info = InstalledDriverInfo(
                    name=driver_info.get("name", driver_name),
                    description=driver_info.get("description", ""),
                    driverPath=driver_info.get("driverPath", f"{self._prefix}{driver_name}.driver"),
                    driverClass=driver_info.get("driverClass", f"{driver_name.capitalize()}Driver"),
                    packageName=dist.name
                )
                self._installed_drivers[installed_driver_info.name] = installed_driver_info


class AvailableDriverRegistry(ABC):
    def __init__(self, installed_registry: InstalledDriverRegistry) -> None:
        self._registry: Dict[str, DriverRegistryInfo] = {}
        self._installed_registry = installed_registry

    def get_driver_info(self, driver_name: str) -> DriverRegistryInfo:
        self._load_registry()
        if driver_name not in self._registry:
            raise KeyError(f"Driver '{driver_name}' not found in the registry")
        return self._registry[driver_name]

    def get_available_drivers_info(self) -> Dict[str, DriverRegistryInfo]:
        self._load_registry()
        return {
            name: info
            for name, info in self._registry.items()
            if not self._installed_registry.is_driver_installed(name)
        }

    def _load_registry(self) -> None:
        registry_json = self._get_registry_json()
        self._registry = {}
        for driver in registry_json['drivers']:
            driver_info = DriverRegistryInfo(**driver)
            self._registry[driver_info.name] = driver_info

    def _get_registry_json(self) -> Dict[str, Any]:
        raise NotImplementedError


class LocalAvailableDriverRegistry(AvailableDriverRegistry):
    def __init__(self, registry_json_filepath: str, installed_registry: InstalledDriverRegistry) -> None:
        self._registry_filepath = registry_json_filepath
        super().__init__(installed_registry)

    def _get_registry_json(self) -> Dict[str, Any]:
        if os.path.exists(self._registry_filepath):
            with open(self._registry_filepath, 'r') as file:
                return json.load(file)
        else:
            raise FileNotFoundError(f"Registry file '{self._registry_filepath}' not found")


class RemoteAvailableDriverRegistry(AvailableDriverRegistry):
    def __init__(self, registry_json_url: str, installed_registry: InstalledDriverRegistry) -> None:
        self._registry_url = registry_json_url
        super().__init__(installed_registry)

    def _get_registry_json(self) -> Dict[str, Any]:
        response = requests.get(self._registry_url)
        if response.status_code == 200:
            return response.json()
        raise ConnectionError(f"Failed to load driver registry from {self._registry_url}")


class DriverInstaller:
    '''
    Responsible for installing and uninstalling drivers using pip.
    '''
    def __init__(self, installed_registry: InstalledDriverRegistry) -> None:
        self._installed_registry = installed_registry

    def install_driver(self, driver_name: str, driver_repo_url: str) -> None:
        '''
        Installs the driver using pip and registers it in the installed registry.
        '''
        try:
            # Install from the given repository URL (e.g., GitHub)
            install_command = [sys.executable, "-m", "pip", "install", f"git+{driver_repo_url}"]

            # Run the pip install command
            subprocess.check_call(install_command)
            print(f"Driver '{driver_name}' installed successfully.")

            # refresh installed drivers
            self._installed_registry.refresh()

        except subprocess.CalledProcessError as e:
            print(f"Error occurred during installation of driver '{driver_name}': {e}")
            raise

    def uninstall_driver(self, driver_name: str) -> None:
        '''
        Uninstalls the driver using pip.
        '''
        if not self._installed_registry.is_driver_installed(driver_name):
            raise KeyError(f"Driver '{driver_name}' is not installed.")

        driver_info = self._installed_registry.get_installed_driver_info(driver_name)
        package_name = driver_info.packageName

        try:
            uninstall_command = [sys.executable, "-m", "pip", "uninstall", "-y", package_name]
            subprocess.check_call(uninstall_command)
            print(f"Driver '{driver_name}' uninstalled successfully.")

        except subprocess.CalledProcessError as e:
            print(f"Failed to uninstall driver '{driver_name}': {e}")
            raise
        self._installed_registry.refresh()


class DriverLoader:
    '''
    Loads drivers that are installed via pip.
    '''
    def load_driver(self, driver_name: str, installed_registry: InstalledDriverRegistry) -> IDriver:
        try:
            # Retrieve driver info from the installed registry
            if not installed_registry.is_driver_installed(driver_name):
                raise KeyError(f"Driver '{driver_name}' is not installed.")

            driver_info = installed_registry._installed_drivers[driver_name]

            # Dynamically load the driver module and class from driverPath and driverClass
            driver_module = importlib.import_module(driver_info.driverPath)
            driver_class = getattr(driver_module, driver_info.driverClass)

            if not issubclass(driver_class, IDriver):
                raise TypeError(f"Class '{driver_info.driverClass}' does not implement IDriver.")
            args = {"name": driver_name}
            return driver_class(**args)

        except (ImportError, AttributeError, TypeError) as e:
            raise RuntimeError(f"Error loading driver '{driver_name}': {e}") from e


class IDriverManager(ABC):
    def get_driver(self, driver_name: str) -> IDriver:
        raise NotImplementedError

    def install_driver(self, driver_name: str, driver_repo_url: Optional[str]) -> None:
        raise NotImplementedError

    def get_driver_info(self, driver_name: str) -> InstalledDriverInfo:
        raise NotImplementedError

    def get_installed_drivers_info(self) -> Dict[str, InstalledDriverInfo]:
        raise NotImplementedError

    def get_available_drivers_info(self) -> Dict[str, DriverRegistryInfo]:
        raise NotImplementedError

    def uninstall_driver(self, driver_name: str) -> None:
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
            return self._driver_loader.load_driver(driver_name, self._installed_registry)
        else:
            raise KeyError(f"Driver '{driver_name}' is not installed")

    def get_driver_info(self, driver_name: str) -> InstalledDriverInfo:
        return self._installed_registry.installed_drivers[driver_name]

    def get_installed_drivers_info(self) -> Dict[str, InstalledDriverInfo]:
        return self._installed_registry.installed_drivers

    def get_available_drivers_info(self) -> Dict[str, DriverRegistryInfo]:
        return self._driver_registry.get_available_drivers_info()

    def install_driver(self, driver_name: str, driver_repo_url: Optional[str]) -> None:
        if driver_repo_url is None:
            driver_info = self._driver_registry.get_driver_info(driver_name)
            driver_repo_url = driver_info.url

        self._driver_installer.install_driver(driver_name, driver_repo_url)

    def uninstall_driver(self, driver_name: str) -> None:
        self._driver_installer.uninstall_driver(driver_name)
