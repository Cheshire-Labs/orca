"""Physical topology for the Opentrons Flex SMC assay example.

The SMC bench that the Hamilton example (``examples/hamilton_smc``) models with
two ML STARs, re-instrumented with two Opentrons Flex liquid handlers:

  * ``mlstar_1`` -> ``flex_1``  (DDR-1 zone, capture + detection)
  * ``mlstar_2`` -> ``flex_2``  (DDR-3 zone, elution + neutralize + read)

The SMC assay routes a fresh tip box around the system map per pipetting action
(stacker -> deck -> waste). On a Hamilton that is physically impossible: tips are
racks that clip into human-loaded tip carriers, so a transporter arm cannot
deliver one to a deck site. On an Opentrons a tip box sits on a flat deck slot
exactly like a plate, so the arm can pick and place it. The Flex (not the OT-2)
is the target because relaying a dropped box from the handoff slot to the working
slot needs the liquid handler's own internal gripper, which the Flex has and the
OT-2 does not.

The shared devices (3 DDR zones, 2 translator bridges, 8 stackers, 10 shakers,
9 parking pads, biotek washers, centrifuge, sealer, delidder, reader, plate
hotel, waste) match the Hamilton SMC topology. This bench additionally carries 4
thermocyclers (available for a TruSeq library-prep workflow; the SMC assay does
not use them).

Topology = devices + locations + transporters + multi-device pools; it is
independent of any particular workflow. ``build_topology()`` returns a fresh
``Topology`` on every call so successive runs never share device or driver
state.

Devices use the no-driver SDK constructor; the bound device factory supplies the
(live, sim) driver pair. The daemon binds ``RemoteDeviceFactory``, which makes every
device dispatch to orca-client over the WebSocket. Tests pass a factory of their own.
"""


from orca.sdk.devices import (
    Centrifuge,
    Delidder,
    LiquidHandler,
    PlatePad,
    PlateWasher,
    Reader,
    ResourcePool,
    Sealer,
    Shaker,
    Storage,
    Thermocycler,
    Translator,
    Transporter,
    Waste,
)
from orca.sdk.build import Topology


SYSTEM_NAME = "Opentrons Flex SMC Assay"
SYSTEM_DESCRIPTION = (
    "SMC immunoassay authoring real inline liquid-handler methods "
    "(aspirate/dispense/tips) pushed via PyLabRobot on an Opentrons "
    "Flex pair (flex_1 capture/detection, flex_2 elution/neutralize)"
)


def build_topology() -> Topology:
    """Build a fresh Opentrons Flex SMC assay topology (Hamilton SMC bench with
    the two ML STARs replaced by two Flexes).

    Layout: 3 DDR robot zones connected by 2 translator bridges.

      DDR-1 (zone 1): biotek_1, flex_1, waste_1, pads 1-3
        |
      translator_1
        |
      DDR-2 (zone 2): centrifuge, sealer, delidder, stackers 1-8, shakers 1-10, thermocyclers 1-4, pads 4-6
        |
      translator_2
        |
      DDR-3 (zone 3): biotek_2, flex_2, plate_hotel, smc_pro, waste_2, pads 7-9
    """

    biotek_1 = PlateWasher("biotek_1")
    biotek_2 = PlateWasher("biotek_2")
    flex_1 = LiquidHandler(
        "flex_1",
        deck_layout="default",
    )
    flex_2 = LiquidHandler(
        "flex_2",
        deck_layout="default",
    )
    sealer = Sealer("sealer")
    centrifuge_device = Centrifuge("centrifuge")
    plate_hotel = Storage("plate_hotel")
    delidder = Delidder("delidder")
    smc_pro = Reader("smc_pro")
    waste_1 = Waste("waste_1")
    waste_2 = Waste("waste_2")

    stackers = {f"stacker_{i}": Storage(f"stacker_{i}") for i in range(1, 9)}
    shakers = {f"shaker_{i}": Shaker(f"shaker_{i}") for i in range(1, 11)}
    thermocyclers = {f"thermocycler_{i}": Thermocycler(f"thermocycler_{i}") for i in range(1, 5)}

    # General-purpose parking pads (deadlock resolution enabled by default):
    # the move resolver stages a blocked plate here to free a route. pads 1-3
    # on ddr_1, 4-6 on ddr_2, 7-9 on ddr_3 (matching their teachpoint zones).
    parking_pads = {f"pad_{i}": PlatePad(f"pad_{i}") for i in range(1, 10)}

    # Translator pads: deadlock resolution disabled on bridge endpoints so
    # the resolver does not try to free a plate by moving it off the bridge.
    translator_1_start = PlatePad("translator_1_start", supports_deadlock_resolution=False)
    translator_1_end = PlatePad("translator_1_end", supports_deadlock_resolution=False)
    translator_2_start = PlatePad("translator_2_start", supports_deadlock_resolution=False)
    translator_2_end = PlatePad("translator_2_end", supports_deadlock_resolution=False)

    ddr_1 = Transporter("ddr_1")
    ddr_2 = Transporter("ddr_2")
    ddr_3 = Transporter("ddr_3")
    # A translator is ONE carriage on a rail: start/end are the same pad
    # at two positions.
    translator_1 = Translator("translator_1")
    translator_2 = Translator("translator_2")

    shaker_collection = ResourcePool("shaker_collection", list(shakers.values()))

    locations = {
        "biotek_1": biotek_1, "biotek_2": biotek_2,
        "flex_1": flex_1, "flex_2": flex_2,
        "sealer": sealer, "centrifuge": centrifuge_device,
        "plate_hotel": plate_hotel, "delidder": delidder,
        "smc_pro": smc_pro,
        "waste_1": waste_1, "waste_2": waste_2,
        **stackers,
        **shakers,
        **thermocyclers,
        **parking_pads,
        "translator_1_start": translator_1_start, "translator_1_end": translator_1_end,
        "translator_2_start": translator_2_start, "translator_2_end": translator_2_end,
    }

    return Topology(
        locations=locations,
        transporters=[ddr_1, ddr_2, ddr_3, translator_1, translator_2],
        pools=[shaker_collection],
    )
