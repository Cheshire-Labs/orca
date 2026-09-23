"""Topology factories that fail to mount, for the daemon's error-path tests.

One fails in `build_system` and the other in `SystemRuntime.start()`. The
daemon handles the two in different places, so each needs its own case.
"""

from orca.resource_models.devices import Device
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology
from tests.mock import UniversalSimDriver
from tests.test_helpers import create_test_device, create_test_transporter


class DeviceWithNoKind(Device):
    """Every device class must declare KIND, and the runtime reads it at start."""


def build_topology_with_an_unknown_teachpoint(stores: IRuntimeStoreFactory) -> Topology:
    """The arm is taught a location that nothing registers. The builder refuses it."""
    del stores
    shaker = create_test_device("shaker1")
    arm = create_test_transporter("robot1", ["shaker1", "pad1", "nowhere"])
    return Topology(
        locations={"shaker1": shaker, "pad1": PlatePad("pad1")},
        transporters=[arm],
        pools=[ResourcePool("shaker1", [shaker])],
    )


def build_topology_with_a_device_that_has_no_kind(stores: IRuntimeStoreFactory) -> Topology:
    """Builds, then fails in start."""
    del stores
    driver = UniversalSimDriver("thing")
    thing = DeviceWithNoKind("thing", driver, driver)
    arm = create_test_transporter("robot1", ["thing", "pad1"])
    return Topology(
        locations={"thing": thing, "pad1": PlatePad("pad1")},
        transporters=[arm],
        pools=[ResourcePool("thing", [thing])],
    )
