import pytest
from unittest.mock import MagicMock

from orca.config_interfaces import IResourceConfig

from orca_driver_interface.driver_interfaces import ILabwarePlaceableDriver
from orca_driver_interface.transporter_interfaces import ITransporterDriver
from orca.driver_management.driver_installer import DriverManager
from orca.yml_config_builder.resource_factory import ResourceFactory


@pytest.fixture
def driver_manager():
    return MagicMock(spec=DriverManager)

@pytest.fixture
def resource_factory(driver_manager):
    return ResourceFactory(filepath_reconciler=MagicMock(), driver_manager=driver_manager)

def test_create_with_labware_placeable_driver(resource_factory: MagicMock, driver_manager: MagicMock):
    # Mock a LabwarePlaceableDriver
    mock_driver = MagicMock(spec=ILabwarePlaceableDriver)
    driver_manager.get_driver.return_value = mock_driver

    # Create a resource config for the mock driver
    config = MagicMock(spec=IResourceConfig)
    config.type = 'mock-labware-loadable'
    config.sim = False
    config.options = {}
    equipment = resource_factory.create("Mock Labware Loadable", config)

    # Assertions
    assert equipment is not None
    assert isinstance(equipment._driver, ILabwarePlaceableDriver)

def test_create_with_transporter_driver(resource_factory: MagicMock, driver_manager: MagicMock):
    # Mock a TransporterDriver
    mock_driver = MagicMock(spec=ITransporterDriver)
    driver_manager.get_driver.return_value = mock_driver

    # Create a resource config for the mock driver
    config = MagicMock(spec=IResourceConfig)
    config.type = 'mock-robot'
    config.sim = False
    config.options = {}
    equipment = resource_factory.create('DDR', config)

    # Assertions
    assert equipment is not None
    assert isinstance(equipment._driver, ITransporterDriver)

def test_create_with_unknown_resource_type(resource_factory: MagicMock):
    # Test with an unknown resource type
    config = MagicMock(spec=IResourceConfig)
    config.type = 'unknown-type'
    config.sim = False
    config.options = {}
    with pytest.raises(ValueError):
        resource_factory.create('UnknownResource', config)
