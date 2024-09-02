from io import BytesIO
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch
import pytest
from orca.driver_management.driver_installer import DriverInstaller


from typing import Generator

@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    # Setup: Create a temporary directory
    dir_path = tempfile.mkdtemp()

    # Yield the directory path to the test
    yield dir_path

    # Teardown: Remove the temporary directory and all its contents
    shutil.rmtree(dir_path)

@pytest.fixture
def driver_installer(temp_dir: str) -> DriverInstaller:
    return DriverInstaller(temp_dir)

@patch('orca.driver_management.driver_installer.ZipFile')
@patch('orca.driver_management.driver_installer.requests.get') 
def test_download_and_extract_driver(mock_get: MagicMock, mock_zipfile: MagicMock, temp_dir: str, driver_installer: DriverInstaller):
    driver_name = 'test_driver'
    driver_repo_url = 'https://github.com/test/test_driver'
    branch = 'main'

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raw = BytesIO(b"Test data as bytes")
    mock_get.return_value.__enter__.return_value = mock_response

        
    extract_path = os.path.join(temp_dir, driver_name)
    mock_zipfile.return_value.__enter__.return_value.extractall = MagicMock()
    mock_zipfile.return_value.__enter__.return_value.extractall.return_value = extract_path

    result = driver_installer.download_and_extract_driver(driver_name, driver_repo_url, branch)

    assert result == extract_path
    mock_get.assert_called_once_with(f"{driver_repo_url}/archive/refs/heads/{branch}.zip", stream=True)
    mock_zipfile.assert_called_once_with(os.path.join(driver_installer._drivers_dir, f"{driver_name}.zip"), 'r')
    mock_zipfile.return_value.__enter__.return_value.extractall.assert_called_once_with(extract_path)


def test_uninstall_driver(temp_dir: str, driver_installer: DriverInstaller):
    driver_name = 'test_driver'

    driver_installer._drivers_dir = temp_dir
    driver_path = os.path.join(temp_dir, driver_name)
    os.makedirs(driver_path)

    driver_installer.uninstall_driver(driver_name)

    assert not os.path.exists(driver_path)