from abc import ABC
import importlib
import subprocess
from pydantic import BaseModel, Field
import json
import os
import sys
import tomllib
from typing import Any, Dict, List, Optional, Type, TypeVar
from urllib.parse import urlparse
import requests

from orca_driver_interface.driver_interfaces import IDriver


class CommandParamSchema(BaseModel):
    name: str
    type: str = 'string'
    description: str = ''
    required: bool = False
    default: Optional[Any] = None
    

class CommandSchema(BaseModel):
    name: str
    description: str = ''
    params: List[CommandParamSchema] = Field(default_factory=list)


class DriverInfo(BaseModel):
    name: str
    driverPath: str  
    driverClass: str  
    description: str = ''

class InstalledDriverInfo(DriverInfo):
    packageName: str


class DriverRegistryInfo(BaseModel):
    name: str
    description: str
    url: str

class AvailableDriverRegistry(ABC):
    def __init__(self) -> None:
        self._registry: Dict[str, DriverRegistryInfo] = {}
        
    def get_driver_info(self, driver_name: str) -> DriverRegistryInfo:
        self._load_registry()
        if driver_name not in self._registry:
            raise KeyError(f"Driver '{driver_name}' not found in the registry")
        return self._registry[driver_name]
    
    
    def get_available_drivers_info(self) -> Dict[str, DriverRegistryInfo]:
        self._load_registry()
        return self._registry
    
    def _load_registry(self) -> None:
        registry_json = self._get_registry_json()
        self._registry = {}
        for driver in registry_json['drivers']:
            driver_info = DriverRegistryInfo(**driver)
            self._registry[driver_info.name] = driver_info
        
    def _get_registry_json(self) -> Dict[str, Any]:
        raise NotImplementedError
    

class LocalAvailableDriverRegistry(AvailableDriverRegistry):
    def __init__(self, registry_json_filepath: str) -> None:
        self._registry_filepath = registry_json_filepath
        super().__init__()

    def _get_registry_json(self) -> Dict[str, Any]:
        if os.path.exists(self._registry_filepath):
            with open(self._registry_filepath, 'r') as file:
                 return json.load(file)
        else:
            raise FileNotFoundError(f"Registry file '{self._registry_filepath}' not found")

class RemoteAvailableDriverRegistry(AvailableDriverRegistry):
    def __init__(self, registry_json_url: str) -> None:
        self._registry_url = registry_json_url
        super().__init__()

    def _get_registry_json(self) -> Dict[str, Any]:
        response = requests.get(self._registry_url)
        if response.status_code == 200:
            return response.json()
        else:
            raise ConnectionError(f"Failed to load driver registry from {self._registry_url}")



class InstalledDriverRegistry:
    @property
    def installed_drivers(self) -> Dict[str, InstalledDriverInfo]:
        return self._installed_drivers

    def __init__(self, installed_registry_filepath: str) -> None:
        self._installed_registry_filepath = installed_registry_filepath
        self._create_registry_if_not_exists()
        self._installed_drivers: Dict[str, InstalledDriverInfo] = {}
        self._load_installed_drivers()
    
    def _create_registry_if_not_exists(self) -> None:
        directory = os.path.dirname(self._installed_registry_filepath)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

        if not os.path.exists(self._installed_registry_filepath):
            with open(self._installed_registry_filepath, 'w') as file:
                json.dump({}, file)
    
    def get_installed_driver_info(self, driver_name: str) -> InstalledDriverInfo:
        return self.installed_drivers[driver_name]

    def save_installed_drivers(self) -> None:
        json_dump: Dict[str, Any] = {name: info.model_dump() for name, info in self._installed_drivers.items()}
        with open(self._installed_registry_filepath, 'w') as file:
            json.dump(json_dump, file, indent=2)

    def is_driver_installed(self, driver_name: str) -> bool:
        return driver_name in self._installed_drivers

    def add_driver(self, driver_name: str, driver_info: InstalledDriverInfo) -> None:
        self._installed_drivers[driver_name] = driver_info
        self.save_installed_drivers()

    def remove_driver(self, driver_name: str) -> None:
        if driver_name in self._installed_drivers:
            del self._installed_drivers[driver_name]
            self.save_installed_drivers()

    def _load_installed_drivers(self) -> None:
        with open(self._installed_registry_filepath, 'r') as file:
            installed_drivers_json = json.load(file)
        for driver_name, driver_info in installed_drivers_json.items():
            driver_info = InstalledDriverInfo(**driver_info)
            self._installed_drivers[driver_name] = driver_info
         

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
        owner, repo, branch = self._parse_github_url(driver_repo_url)
        driver_infos = self._get_driver_infos(owner, repo, branch)
        package_name = self._get_package_name(owner, repo, branch)
        try:
            # Install from the given repository URL (e.g., GitHub)
            install_command = [sys.executable, "-m", "pip", "install", f"git+{driver_repo_url}"]

            # Run the pip install command
            subprocess.check_call(install_command)
            print(f"Driver '{driver_name}' installed successfully.")

            
            # Add the driver to the installed registry
            for driver_info in driver_infos:
                installed_driver_info = InstalledDriverInfo(packageName=package_name, **driver_info.__dict__)
                self._installed_registry.add_driver(driver_info.name, installed_driver_info)

        except subprocess.CalledProcessError as e:
            print(f"Error occurred during installation of driver '{driver_name}': {e}")
            raise

    def _get_driver_infos(self, owner:str, repo: str, branch: str = "main", driver_info_filename: str = "driver.json") -> List[DriverInfo]:
        driver_info_url = f"https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/{driver_info_filename}"

        response = requests.get(driver_info_url)
        if response.status_code != 200:
            raise Exception(f"Failed to download {driver_info_filename} from {driver_info_url}")
        
        driver_info_json =json.loads(response.text)
        if isinstance(driver_info_json, list):
            driver_info = [DriverInfo(**info_object) for info_object in driver_info_json]
        else:
            driver_info = [DriverInfo(**driver_info_json)]
        
        return driver_info

    def _get_package_name(self, owner: str, repo: str, branch: str = "main", package_filename: str = "pyproject.toml") -> str:
        package_file_url = f"https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/{package_filename}"

        response = requests.get(package_file_url)
        if response.status_code != 200:
            raise Exception(f"Failed to download {package_filename} from {package_file_url}")
        
        # Parse the pyproject.toml content
        pyproject_content = response.text
        pyproject_data = tomllib.loads(pyproject_content)
        package_name = pyproject_data.get('project', {}).get('name', None)
    
        if not package_name:
            raise Exception("Package name not found in {package_filename}")
        
        return package_name

    def _parse_github_url(self, github_repo_url: str):
        # Handle case where the branch is specified after '@'
        if '@' in github_repo_url:
            repo_url, branch = github_repo_url.split('@')
        else:
            repo_url = github_repo_url
            branch = "main"  # Default to 'main' branch if not specified
        
        # Parse the GitHub URL to extract the owner and repo name
        parsed_url = urlparse(repo_url)
        path_parts = parsed_url.path.strip('/').split('/')
        
        if len(path_parts) < 2:
            raise ValueError(f"Invalid GitHub repository URL: {github_repo_url}")
        
        # Extract the owner and repo name
        owner = path_parts[0]
        repo = path_parts[1].replace('.git', '')  # Remove '.git' from repo name if present
    
        return owner, repo, branch
    
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
        self._installed_registry.remove_driver(driver_name)

T = TypeVar('T', bound=IDriver)

class DriverLoader:
    '''
    Loads drivers that are installed via pip.
    '''
    def load_driver(self, driver_name: str, installed_registry: InstalledDriverRegistry, driver_type: Type[T]) -> T:
        try:
            # Retrieve driver info from the installed registry
            if not installed_registry.is_driver_installed(driver_name):
                raise KeyError(f"Driver '{driver_name}' is not installed.")

            driver_info = installed_registry._installed_drivers[driver_name]

            # Dynamically load the driver module and class from driverPath and driverClass
            driver_module = importlib.import_module(driver_info.driverPath)
            driver_class = getattr(driver_module, driver_info.driverClass)

            if not issubclass(driver_class, driver_type):
                raise TypeError(f"Class '{driver_info.driverClass}' does not implement IDriver.")

            return driver_class()

        except (ImportError, AttributeError, TypeError) as e:
            raise RuntimeError(f"Error loading driver '{driver_name}': {e}") from e

class IDriverManager(ABC):
    def get_driver(self, driver_name: str, driver_type: Type[T]) -> T:
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

    def get_driver(self, driver_name: str, driver_type: Type[T]) -> T:
        if self._installed_registry.is_driver_installed(driver_name):
            return self._driver_loader.load_driver(driver_name, self._installed_registry, driver_type)
        else:
            raise KeyError(f"Driver '{driver_name}' is not installed")

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
        
