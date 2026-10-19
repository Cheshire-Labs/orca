"""Physical topology for the PLR example.

Topology = devices + locations + transporters + multi-device pools. This is
the lab's physical layout; it is independent of any particular workflow.

``build_topology()`` returns a fresh ``Topology`` on every call. Per-call
freshness matters for test isolation -- two runs of the same test must never
share device objects or their driver state.

Devices use the no-driver SDK constructor; the bound device factory supplies the
(live, sim) driver pair. ``SIM_DRIVERS`` gives the liquid handler a
``ChatterboxLiquidHandlerDriver`` when no factory is bound. Tests that need to
inject a custom LH driver (e.g. ``RecordingLiquidHandlerDriver``) bind their own
factory before building; a bound factory always wins.
"""

from collections.abc import Callable

from cheshire_drivers import (
    AccessConfig,
    CartesianCoordinates,
    DeckLayoutConfig,
    DeckResourceConfig,
    Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver

from orca.devices.devices import LiquidHandler, Reader, Storage, Waste
from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology


SYSTEM_NAME = "Learn Orca"
SYSTEM_DESCRIPTION = "Comprehensive example demonstrating every Orca SDK feature"

SIM_DRIVERS: dict[str, Callable[[], DriverPairElement]] = {
    "liquid_handler": lambda: ChatterboxLiquidHandlerDriver(num_channels=8),
}


# Carriers-only deck (structure). Occupancy is NOT declared here: transient
# plates and tip racks materialize via @orca.action(deck_positions=...), and the
# reagent trough is a RESIDENT thread anchored to carrier-25-0. See
# workflows/learn_orca.py.
PLR_DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    """Build a fresh PLR example topology.

    Layout: stacker -- pad_1 -- liquid_handler -- pad_2 -- shaker_pool -- reader -- sealer -- waste.

    Args:
        stores: Factory that produces per-device calibration stores. The
            deployment chooses InMemory (this repo) or DB-backed (a hosted service).
    """
    liquid_handler = LiquidHandler(
        "liquid_handler",
        deck_layout_store=stores.deck_layouts(
            "liquid_handler", seed={"default": PLR_DECK_CONFIG}
        ),
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

    c = CartesianCoordinates
    sealer_access = AccessConfig(
        name="sealer_horizontal",
        access_type="horizontal",
        gripper_offset=25.0,
        horizontal_clearance=120.0,
        vertical_clearance=30.0,
    )
    stores.access_configs(seed=[sealer_access])
    arm_teachpoints = [
        Teachpoint("stacker",        c(0,    200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("pad_1",          c(200,  200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("liquid_handler/carrier-7-2",  c(400,  200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("liquid_handler/carrier-15-1", c(420,  200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("pad_2",          c(600,  200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("shaker_1",       c(800,  200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("shaker_2",       c(900,  200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("reader",         c(1100, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("sealer",         c(1300, 200, 300, 0, 90, 180), orientation="right", access=sealer_access),
        Teachpoint("waste",          c(1500, 200, 200, 0, 90, 180), orientation="right"),
    ]
    robotic_arm = Transporter(
        "robotic_arm",
        teachpoint_store=stores.teachpoints("robotic_arm", seed=arm_teachpoints),
    )

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
