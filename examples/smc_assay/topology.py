"""Physical topology for the SMC assay example.

Topology = devices + locations + transporters + multi-device pools. This is
the lab's physical layout; it is independent of any particular workflow.

``build_topology()`` returns a fresh ``Topology`` on every call. Per-call
freshness matters for test isolation -- two runs of the same test must never
share device objects or their driver state.
"""

from orca.sdk.devices import (
    Centrifuge,
    Delidder,
    LiquidHandlerProtocol,
    PlatePad,
    PlateWasher,
    Reader,
    ResourcePool,
    Sealer,
    Shaker,
    Storage,
    Translator,
    Transporter,
    Waste,
)
from orca.sdk.build import Topology


SYSTEM_NAME = "SMC Assay"

# A Bravo deck has 9 positions. Declaring fewer is not a smaller deck, it is a
# capacity ceiling: a batched run keeps plate_1, neut_plate, tips_384 and the
# shared final_plate resident at once, and a plate with nowhere to land parks
# on the deck forever waiting for co-labware that can never be placed.
_BRAVO_SITES = [f"site-{i}" for i in range(1, 10)]


def build_topology() -> Topology:
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
    ddr_1 = Transporter("ddr_1")
    ddr_2 = Transporter("ddr_2")
    ddr_3 = Transporter("ddr_3")
    translator_1 = Translator("translator_1")
    translator_2 = Translator("translator_2")

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
