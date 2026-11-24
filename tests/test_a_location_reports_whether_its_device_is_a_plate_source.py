from orca.devices.devices import Reader, Storage
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.location import Location


def test_a_location_over_a_bridge_to_a_plate_source_device_is_a_plate_source() -> None:
    location = Location("pad1", LabwareStagingBridge("pad1", Storage("stacker")))
    assert location.is_plate_source


def test_a_location_over_a_bridge_to_a_device_that_is_not_a_plate_source_is_not() -> None:
    location = Location("pad1", LabwareStagingBridge("pad1", Reader("reader")))
    assert not location.is_plate_source


def test_a_location_over_a_plain_pad_is_not_a_plate_source() -> None:
    assert not Location("pad1").is_plate_source


def test_a_location_over_another_location_does_not_ask_the_inner_one() -> None:
    inner = Location("pad1", LabwareStagingBridge("pad1", Storage("stacker")))
    assert not Location("outer", inner).is_plate_source
