import asyncio
import json
import shutil
import tempfile
from typing import Any, Dict, Generator
import pytest
import os
from unittest.mock import patch, MagicMock

from orca.driver_management.driver_installer import DriverInstaller, DriverLoader, DriverManager, LocalAvailableDriverRegistry, InstalledDriverRegistry
from orca_driver_interface.driver_interfaces import IDriver


@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    # Setup: Create a temporary directory
    dir_path = tempfile.mkdtemp()

    # Yield the directory path to the test
    yield dir_path

    # Teardown: Remove the temporary directory and all its contents
    shutil.rmtree(dir_path)

@pytest.fixture
def mock_installed_drivers() -> Dict[str, Any]:
    return {
        "test_driver":
        {
            "name": "test_driver",
            "description": "Test Driver",
            "driverPath": "src.mock_driver.py",
            "driverClass": "MockDriver",
            "commands" : [
                {
                    "name": "test_command",
                    "description": "Test Command",
                    "parameters": [
                        {
                            "name": "param1",
                            "description": "Test Parameter 1",
                            "type": "string",
                            "required": True
                        }
                    ]
                }
            ],
            "return_type": "string"
        }   
    }

@pytest.fixture
def mock_available_drivers() -> Dict[str, Any]:
    return {
    "drivers": [
        {
            "name": "test_driver",
            "description": "Test Driver",
            "repository": "https://github.com/cheshire-labs/test-driver.git"
        }
    ]
}


@pytest.fixture
def driver_manager(temp_dir: str, mock_available_drivers: Dict[str, Any], mock_installed_drivers: Dict[str, Any]) -> DriverManager:
    
    available_driver_registry = os.path.join(temp_dir, 'mock_available_drivers.json')
    with open(available_driver_registry, 'w') as f:
        json.dump(mock_available_drivers, f)

    os.makedirs(os.path.join(temp_dir, 'test_driver'))
    shutil.copyfile(os.path.join(os.path.dirname(__file__), 'resources', 'test_driver.py'), os.path.join(temp_dir, 'test_driver', 'test_driver.py'))
    installed_registry = InstalledDriverRegistry()
    installer = DriverInstaller(installed_registry)
    loader = DriverLoader()
    driver_registry = LocalAvailableDriverRegistry(available_driver_registry)
    return DriverManager(installed_registry, loader, installer, driver_registry)


def test_load_driver_success(driver_manager: DriverManager):
    driver: IDriver = driver_manager.get_driver('test_driver')
    
    assert driver is not None
    assert isinstance(driver, IDriver)
    # Further tests to ensure the driver behaves correctly
    asyncio.run(driver.initialize())
    asyncio.run(driver.execute('test_command', {}))
    
