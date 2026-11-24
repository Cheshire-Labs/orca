"""Physical topology for the PLR example.

Topology = devices + locations + transporters + multi-device pools. This is
the lab's physical layout; it is independent of any particular workflow.

``build_topology()`` returns a fresh ``Topology`` on every call. Per-call
freshness matters for test isolation -- two runs of the same test must never
share device objects or their driver state.

Devices use the no-driver SDK constructor; the bound device factory supplies the
(live, sim) driver pair. The daemon binds ``RemoteDeviceFactory``, which makes every
device dispatch to orca-client over the WebSocket. Tests pass a factory of their own.
"""


from orca.sdk.devices import (
    LiquidHandler,
    PlatePad,
    Reader,
    ResourcePool,
    Sealer,
    Shaker,
    Storage,
    Transporter,
    Waste,
)
from orca.sdk.build import Topology


SYSTEM_NAME = "Learn Orca"
SYSTEM_DESCRIPTION = "Comprehensive example demonstrating every Orca SDK feature"


def build_topology() -> Topology:
    """Build a fresh PLR example topology.

    Layout: stacker -- pad_1 -- liquid_handler -- pad_2 -- shaker_pool -- reader -- sealer -- waste.
    """
    liquid_handler = LiquidHandler(
        "liquid_handler",
        deck_layout="default",
    )
    reader = Reader("reader")
    sealer = Sealer("sealer")
    shaker_1 = Shaker("shaker_1")
    shaker_2 = Shaker("shaker_2")
    stacker = Storage("stacker")
    waste = Waste("waste")

    pad_1 = PlatePad("pad_1")
    pad_2 = PlatePad("pad_2")

    shaker_pool = ResourcePool("shaker_pool", [shaker_1, shaker_2])

    robotic_arm = Transporter("robotic_arm")

    locations = {
        "stacker": stacker,
        "pad_1": pad_1,
        "liquid_handler": liquid_handler,
        "pad_2": pad_2,
        "shaker_1": shaker_1,
        "shaker_2": shaker_2,
        "reader": reader,
        "sealer": sealer,
        "waste": waste,
    }

    return Topology(
        locations=locations,
        transporters=[robotic_arm],
        pools=[shaker_pool],
    )
