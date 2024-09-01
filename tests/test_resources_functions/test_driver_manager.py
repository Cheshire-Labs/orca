import asyncio
import pytest
import os
from unittest.mock import patch, MagicMock

from orca.driver_management.driver_installer import DriverManager
from orca_driver_interface.driver_interfaces import IDriver

@pytest.fixture
def driver_manager() -> DriverManager:
    
    return DriverManager()


def test_load_driver_from_directory(driver_manager: DriverManager):
    driver: IDriver = driver_manager.load_driver('test_driver', 'test_driver')
    
    assert driver is not None
    assert isinstance(driver, IDriver)
    # Further tests to ensure the driver behaves correctly
    asyncio.run(driver.initialize())
    asyncio.run(driver.execute('test_command', {}))
    
@patch('os.path.isdir', return_value=True)
@patch('os.path.isfile', return_value=True)
@patch('driver_manager.import_module')
def test_load_driver_success(mock_import_module, mock_isfile, mock_isdir, driver_manager: DriverManager):
    # Mock the driver class and module
    mock_driver_class = MagicMock(spec=IDriver)
    mock_module = MagicMock()
    mock_module.MockInstalledDriverDriver = mock_driver_class
    mock_import_module.return_value = mock_module

    # Test loading the driver
    driver = driver_manager.load_driver('mock_installed', 'test_driver')
    
    assert driver is not None
    assert isinstance(driver, IDriver)
    mock_driver_class.assert_called_once()

@patch('os.path.isdir', return_value=False)
def test_load_driver_directory_not_found(mock_isdir, driver_manager: DriverManager):
    with pytest.raises(FileNotFoundError) as exc_info:
        driver_manager.load_driver('non_existent_driver', 'test_driver')
    
    assert "Driver directory" in str(exc_info.value)

@patch('os.path.isdir', return_value=True)
@patch('os.path.isfile', return_value=False)
def test_load_driver_module_not_found(mock_isfile, mock_isdir, driver_manager: DriverManager):
    with pytest.raises(FileNotFoundError) as exc_info:
        driver_manager.load_driver('driver_with_no_file', 'test_driver')
    
    assert "Driver module file" in str(exc_info.value)

@patch('os.path.isdir', return_value=True)
@patch('os.path.isfile', return_value=True)
@patch('driver_manager.import_module')
def test_load_driver_class_not_found(mock_import_module, mock_isfile, mock_isdir, driver_manager: DriverManager):
    mock_module = MagicMock()
    mock_import_module.return_value = mock_module
    
    with pytest.raises(AttributeError) as exc_info:
        driver_manager.load_driver('driver_with_wrong_class_name', 'test_driver')
    
    assert "Class 'DriverWithWrongClassNameDriver' not found" in str(exc_info.value)

@patch('os.path.isdir', return_value=True)
@patch('os.path.isfile', return_value=True)
@patch('driver_manager.import_module')
def test_load_driver_class_does_not_implement_interface(mock_import_module, mock_isfile, mock_isdir, driver_manager: DriverManager):
    mock_class = MagicMock()
    mock_module = MagicMock()
    mock_module.DriverWithNoInterfaceDriver = mock_class
    mock_import_module.return_value = mock_module
    
    with pytest.raises(TypeError) as exc_info:
        driver_manager.load_driver('driver_with_no_interface', 'test_driver')
    
    assert "does not implement IDriver" in str(exc_info.value)