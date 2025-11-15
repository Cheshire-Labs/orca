"""
Test helper utilities for Orca tests.

Provides common utilities for building test systems, creating test data,
and waiting for async operations to complete.
"""
import asyncio
from typing import List, Dict
from orca.resource_models.resource_extras.teachpoints import Teachpoint, CartesianCoordinates
from orca.resource_models.transporter import Transporter
from orca.resource_models.devices import Device
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.labware import PlateTemplate, LabwareInstance
from orca.driver_management.drivers.sims import SimTransporterDriver, SimDriver
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.status_enums import LabwareThreadStatus
from pylabrobot.resources.corning.falcon.plates import Cor_Falcon_96_wellplate_340ul_Fb_Black
from tests.mock import UniversalMockDevice


def create_test_teachpoints(names: List[str]) -> List[Teachpoint]:
    """
    Create simple teachpoints for testing.

    Args:
        names: List of teachpoint names

    Returns:
        List of Teachpoint objects with simple coordinates
    """
    teachpoints = []
    for i, name in enumerate(names):
        coords = CartesianCoordinates(
            x=i * 100.0,
            y=0.0,
            z=0.0,
            yaw=0.0,
            pitch=0.0,
            roll=0.0
        )
        teachpoint = Teachpoint(
            name=name,
            coordinates=coords,
            orientation=None,
            access_type="vertical"
        )
        teachpoints.append(teachpoint)
    return teachpoints


def create_test_transporter(
    name: str,
    location_names: List[str]
) -> Transporter:
    """
    Create a test transporter with simple teachpoints.

    Args:
        name: Transporter name
        location_names: Names of locations this transporter can reach

    Returns:
        Configured Transporter instance
    """
    teachpoints = create_test_teachpoints(location_names)
    driver = SimTransporterDriver(name)
    transporter = Transporter(name, driver, load_positions=teachpoints, sim=True)
    return transporter


def create_test_device(name: str, device_type: str = "device") -> UniversalMockDevice:
    """
    Create a test device that supports all action interfaces.

    Args:
        name: Device name
        device_type: Type of device for simulation (deprecated, kept for compatibility)

    Returns:
        UniversalMockDevice instance supporting all action types
    """
    return UniversalMockDevice(name)


def create_test_plate_template(name: str = "test_plate") -> PlateTemplate:
    """
    Create a test plate template.

    Args:
        name: Plate template name

    Returns:
        PlateTemplate instance
    """
    return PlateTemplate(name, Cor_Falcon_96_wellplate_340ul_Fb_Black)


def create_test_labware_instance(name: str = "test_plate") -> LabwareInstance:
    """
    Create a test labware instance.

    Args:
        name: Labware name

    Returns:
        LabwareInstance
    """
    template = create_test_plate_template(name)
    return LabwareInstance(template, f"{name}_instance")


def create_simple_system_map(
    transporters: List[Transporter],
    devices: Dict[str, Device],
    parking_pads: Dict[str, PlatePad] = None
) -> tuple[ResourceRegistry, SystemMap]:
    """
    Create a simple system map for testing.

    Args:
        transporters: List of transporter resources
        devices: Dict of device_name -> Device
        parking_pads: Optional dict of pad_name -> PlatePad

    Returns:
        Tuple of (ResourceRegistry, SystemMap)
    """
    registry = ResourceRegistry()

    # Add transporters
    for transporter in transporters:
        registry.add_resource(transporter)

    # Add devices
    for device in devices.values():
        registry.add_resource(device)

    # Create system map
    system_map = SystemMap(registry)

    # Assign devices to locations
    system_map.assign_resources(devices)

    # Add parking pads if provided
    if parking_pads:
        system_map.assign_resources(parking_pads)

    return registry, system_map


async def wait_for_thread_status(
    thread: ExecutingLabwareThread,
    status: LabwareThreadStatus,
    timeout: float = 10.0
) -> None:
    """
    Wait for a thread to reach a specific status.

    Args:
        thread: The thread to monitor
        status: The status to wait for
        timeout: Maximum time to wait in seconds

    Raises:
        TimeoutError: If thread doesn't reach status within timeout
    """
    start = asyncio.get_event_loop().time()

    while thread.status != status:
        await asyncio.sleep(0.1)

        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > timeout:
            raise TimeoutError(
                f"Thread {thread.id} did not reach status {status} "
                f"within {timeout}s (current status: {thread.status})"
            )


async def wait_for_threads_completed(
    threads: List[ExecutingLabwareThread],
    timeout: float = 30.0
) -> None:
    """
    Wait for all threads to complete.

    Args:
        threads: List of threads to monitor
        timeout: Maximum time to wait in seconds

    Raises:
        TimeoutError: If any thread doesn't complete within timeout
    """
    tasks = [
        wait_for_thread_status(thread, LabwareThreadStatus.COMPLETED, timeout)
        for thread in threads
    ]
    await asyncio.gather(*tasks)
