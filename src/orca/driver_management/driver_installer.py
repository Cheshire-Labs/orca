from abc import ABC
from dataclasses import dataclass
import json
import os
import shutil
from typing import Dict
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


class IDriverRepoHandler(ABC):
    def get_driver_info(self, driver_name) -> DriverInfo:
        raise NotImplementedError

class DriverRepoHandler(IDriverRepoHandler):
    def __init__(self, drivers_repo='https://your-drivers-repo-url'):
        self._drivers_repo = drivers_repo
        self._registry = self._load_registry()

    def _load_registry(self):
        registry_url = f"{self._drivers_repo}/drivers.json"
        response = requests.get(registry_url)
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to load driver registry from {registry_url}")

    def get_driver_info(self, driver_name):
        for driver in self._registry['drivers']:
            if driver['name'] == driver_name:
                return driver
        return None



class IDriverRegistry(ABC):
    def get_driver(self, driver_name: str) -> IDriver:
        raise NotImplementedError
    
    def add_driver(self, driver: IDriver) -> None:
        raise NotImplementedError



class DriverInstaller:
    '''
    Responsible for installing, uninstalling and updating drivers.
    '''
    def __init__(self, drivers_dir='drivers'):
        self._drivers_dir = drivers_dir
        os.makedirs(self._drivers_dir, exist_ok=True)

    def download_and_extract_driver(self, driver_info, custom_name=None, branch='main'):
        repo_url = driver_info['repository']
        driver_name = custom_name if custom_name else driver_info['name']
        zip_url = f"{repo_url}/archive/refs/heads/{branch}.zip"
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

    def uninstall_driver(self, driver_name):
        driver_path = os.path.join(self._drivers_dir, driver_name)
        if os.path.exists(driver_path):
            shutil.rmtree(driver_path)

class InstalledDriverRegistry:
    def __init__(self, installed_file='installed_drivers.json'):
        self._installed_file = installed_file
        self._installed_drivers = self._load_installed_drivers()

    def _load_installed_drivers(self) -> Dict[str, DriverInfo]:
        if os.path.exists(self._installed_file):
            with open(self._installed_file, 'r') as file:
                return json.load(file)
        else:
            return {}

    def save_installed_drivers(self):
        with open(self._installed_file, 'w') as file:
            json.dump(self._installed_drivers, file, indent=2)

    def is_driver_installed(self, driver_name):
        return driver_name in self._installed_drivers

    def add_driver(self, driver_name, driver_info):
        self._installed_drivers[driver_name] = driver_info
        self.save_installed_drivers()

    def remove_driver(self, driver_name):
        if driver_name in self._installed_drivers:
            del self._installed_drivers[driver_name]
            self.save_installed_drivers()


class IDriverManager(ABC):
    def load_driver(self, driver_name: str) -> IDriver:
        raise NotImplementedError

class DriverManager:
    def __init__(self):
        self._drivers_dir = os.path.join(os.path.dirname(__file__), "drivers")  

    def load_driver(self, driver_name: str, device_name: str) -> IDriver:
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