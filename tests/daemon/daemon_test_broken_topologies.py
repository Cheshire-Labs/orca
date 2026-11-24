"""Topology factories that fail to mount, for the daemon's error-path tests.

One fails in `build_system` and the other in `SystemRuntime.start()`. The
daemon handles the two in different places, so each needs its own case.
"""

from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.resource_models.store_interfaces import IMoveDefaultsStore
from orca.sdk.build import Topology
from tests.setup_package import package_stores, with_teachpoints, write_teachpoints
from tests.test_helpers import create_test_device, create_test_teachpoints, create_test_transporter


class ArmThatRefusesItsMoveDefaults(Transporter):
    """The runtime binds move defaults to every arm at start."""

    def bind_move_defaults(self, store: IMoveDefaultsStore) -> None:
        raise RuntimeError(f"{self.name} refuses its move defaults")


def build_topology_with_an_unknown_teachpoint() -> Topology:
    """The arm is taught a location that nothing registers. The builder refuses it."""
    shaker = create_test_device("shaker1")
    arm = create_test_transporter("robot1", ["shaker1", "pad1", "nowhere"])
    return Topology(
        locations={"shaker1": shaker, "pad1": PlatePad("pad1")},
        transporters=[arm],
        pools=[ResourcePool("shaker1", [shaker])],
    )


def build_topology_with_an_arm_that_fails_at_start() -> Topology:
    """Builds, then fails in start."""
    shaker = create_test_device("shaker1")
    arm = with_teachpoints(
        ArmThatRefusesItsMoveDefaults("robot1"),
        write_teachpoints(package_stores(), "robot1", create_test_teachpoints(["shaker1", "pad1"])),
    )
    return Topology(
        locations={"shaker1": shaker, "pad1": PlatePad("pad1")},
        transporters=[arm],
        pools=[ResourcePool("shaker1", [shaker])],
    )

