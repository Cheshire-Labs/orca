"""Physical topology for the SMC assay example.

Topology = devices + locations + transporters + multi-device pools. This is
the lab's physical layout; it is independent of any particular workflow.

``build_topology()`` returns a fresh ``Topology`` on every call. Per-call
freshness matters for test isolation -- two runs of the same test must never
share device objects or their driver state.
"""

from contextlib import nullcontext

from cheshire_drivers import (
    AccessConfig,
    CartesianCoordinates,
    Teachpoint,
)

from orca.devices.centrifuge import Centrifuge
from orca.devices.devices import Delidder, LiquidHandlerProtocol, PlateWasher, Reader, Storage, Waste
from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.translator import Translator
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_context import is_factory_bound, use_device_factory
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology


# A Bravo deck has 9 positions. Declaring fewer is not a smaller deck, it is a
# capacity ceiling: a batched run keeps plate_1, neut_plate, tips_384 and the
# shared final_plate resident at once, and a plate with nowhere to land parks
# on the deck forever waiting for co-labware that can never be placed.
_BRAVO_SITES = [f"site-{i}" for i in range(1, 10)]


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    """Build a fresh SMC assay topology.

    Layout: 3 DDR robot zones connected by 2 translator bridges.

      DDR-1 (zone 1): biotek_1, bravo_96, waste_1, pads 1-3
        |
      translator_1
        |
      DDR-2 (zone 2): centrifuge, sealer, stackers 1-7, shakers 1-10, pads 4-6
        |
      translator_2
        |
      DDR-3 (zone 3): biotek_2, bravo_384, hotel pads 1-12 (stacked shelves), smc_pro, delidder, waste_2, pads 7-9
    """

    # Teachpoints: taught robot positions for each location.
    # CartesianCoordinates(x, y, z, roll, pitch, yaw) in mm/degrees.
    c = CartesianCoordinates

    # The TeachpointService rejects teachpoints with inline access fields and
    # no named AccessConfig (validate_persistable_access). Two configs cover
    # every teachpoint in this topology.
    default_vertical = AccessConfig(name="default_vertical", access_type="vertical")
    default_horizontal = AccessConfig(name="default_horizontal", access_type="horizontal")
    stores.access_configs(seed=[default_vertical, default_horizontal])


    ddr_1_teachpoints = [
        Teachpoint("biotek_1",            c(100, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("bravo_96",            c(300, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("translator_1_start",  c(450, 200, 300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("waste_1",             c(100, 400, 200, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_1",               c(200, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_2",               c(200, 300, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_3",               c(400, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
    ]

    ddr_2_teachpoints = [
        Teachpoint("centrifuge",          c(600, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("sealer",              c(700, 200, 300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("translator_1_end",    c(600,  50, 300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("translator_2_start",  c(1800, 50, 300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("stacker_1",           c(800,  50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("stacker_2",           c(900,  50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("stacker_3",           c(1000, 50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("stacker_4",           c(1100, 50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("stacker_5",           c(1200, 50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("stacker_6",           c(1300, 50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("stacker_7",           c(1400, 50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("stacker_8",           c(1450, 50, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_1",            c(800,  200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_2",            c(900,  200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_3",            c(1000, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_4",            c(1100, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_5",            c(1200, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_6",            c(800,  400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_7",            c(900,  400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_8",            c(1000, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_9",            c(1100, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("shaker_10",           c(1200, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_4",               c(1500, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_5",               c(1600, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_6",               c(1700, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
    ]

    ddr_3_teachpoints = [
        Teachpoint("biotek_2",            c(1900, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("bravo_384",           c(2100, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("hotel_pad_1",         c(2300, 200, 300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_2",         c(2300, 200, 340, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_3",         c(2300, 200, 380, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_4",         c(2300, 200, 420, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_5",         c(2300, 200, 460, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_6",         c(2300, 200, 500, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_7",         c(2300, 200, 540, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_8",         c(2300, 200, 580, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_9",         c(2300, 200, 620, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_10",         c(2300, 200, 660, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_11",         c(2300, 200, 700, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("hotel_pad_12",         c(2300, 200, 740, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("smc_pro",             c(2300, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("translator_2_end",    c(1900, 50,  300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("waste_2",             c(2400, 100, 200, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("delidder",            c(2000, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_7",               c(2000, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_8",               c(2100, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
        Teachpoint("pad_9",               c(2200, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
    ]

    translator_1_teachpoints = [
        Teachpoint("translator_1_start",  c(0,   0, 300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("translator_1_end",    c(500, 0, 300, 0, 90, 180), orientation="right", access=default_horizontal),
    ]
    translator_2_teachpoints = [
        Teachpoint("translator_2_start",  c(0,   0, 300, 0, 90, 180), orientation="right", access=default_horizontal),
        Teachpoint("translator_2_end",    c(500, 0, 300, 0, 90, 180), orientation="right", access=default_horizontal),
    ]

    # Devices. No-driver SDK ctor: the active factory supplies the (live, sim)
    # pair. Sim drivers only when nothing outer is bound; tests inject custom
    # drivers by binding their own factory via `use_device_factory(...)` before
    # calling `build_topology`.
    ctx = nullcontext() if is_factory_bound() else use_device_factory(SimDeviceFactory())
    with ctx:
        biotek_1 = PlateWasher("biotek_1")
        biotek_2 = PlateWasher("biotek_2")
        # VWorks owns these decks, but each protocol stages 3 labware at once
        # (plate + tips + a second plate), and one position holds one labware.
        bravo_96 = LiquidHandlerProtocol("bravo_96", site_names=_BRAVO_SITES)
        bravo_384 = LiquidHandlerProtocol("bravo_384", site_names=_BRAVO_SITES)
        sealer = Sealer("sealer")
        centrifuge_device = Centrifuge("centrifuge")
        # 12 stacked hotel shelves at one x/y, 40 mm z pitch. Plain pads, not a
        # Storage device; kept OUT of the deadlock-resolution pool (parking spots
        # belong to authors, resolution pads to the engine).
        hotel_pads = {
            f"hotel_pad_{i}": PlatePad(
                f"hotel_pad_{i}", supports_deadlock_resolution=False,
            )
            for i in range(1, 13)
        }
        delidder = Delidder("delidder")
        smc_pro = Reader("smc_pro")
        waste_1 = Waste("waste_1")
        waste_2 = Waste("waste_2")

        stackers = {f"stacker_{i}": Storage(f"stacker_{i}") for i in range(1, 9)}
        shakers = {f"shaker_{i}": Shaker(f"shaker_{i}") for i in range(1, 11)}

        # General-purpose parking pads (deadlock resolution enabled by default): the
        # move resolver stages a blocked plate here to free a route. pads 1-3 on
        # ddr_1, 4-6 on ddr_2, 7-9 on ddr_3 (matching their teachpoint zones).
        parking_pads = {f"pad_{i}": PlatePad(f"pad_{i}") for i in range(1, 10)}

        # Translator pads: deadlock resolution disabled on bridge endpoints so
        # the resolver does not try to free a plate by moving it off the bridge.
        translator_1_start = PlatePad("translator_1_start", supports_deadlock_resolution=False)
        translator_1_end = PlatePad("translator_1_end", supports_deadlock_resolution=False)
        translator_2_start = PlatePad("translator_2_start", supports_deadlock_resolution=False)
        translator_2_end = PlatePad("translator_2_end", supports_deadlock_resolution=False)

        # Transporters. A translator is ONE carriage on a rail: start/end are the
        # same pad at two positions.
        ddr_1 = Transporter(
            "ddr_1",
            teachpoint_store=stores.teachpoints("ddr_1", seed=ddr_1_teachpoints),
        )
        ddr_2 = Transporter(
            "ddr_2",
            teachpoint_store=stores.teachpoints("ddr_2", seed=ddr_2_teachpoints),
        )
        ddr_3 = Transporter(
            "ddr_3",
            teachpoint_store=stores.teachpoints("ddr_3", seed=ddr_3_teachpoints),
        )
        translator_1 = Translator(
            "translator_1",
            teachpoint_store=stores.teachpoints("translator_1", seed=translator_1_teachpoints),
        )
        translator_2 = Translator(
            "translator_2",
            teachpoint_store=stores.teachpoints("translator_2", seed=translator_2_teachpoints),
        )

    shaker_collection = ResourcePool("shaker_collection", list(shakers.values()))

    locations = {
        "biotek_1": biotek_1, "biotek_2": biotek_2,
        "bravo_96": bravo_96, "bravo_384": bravo_384,
        "sealer": sealer, "centrifuge": centrifuge_device,
        **hotel_pads, "delidder": delidder,
        "smc_pro": smc_pro,
        "waste_1": waste_1, "waste_2": waste_2,
        **stackers,
        **shakers,
        **parking_pads,
        "translator_1_start": translator_1_start, "translator_1_end": translator_1_end,
        "translator_2_start": translator_2_start, "translator_2_end": translator_2_end,
    }

    return Topology(
        locations=locations,
        transporters=[ddr_1, ddr_2, ddr_3, translator_1, translator_2],
        pools=[shaker_collection],
    )
