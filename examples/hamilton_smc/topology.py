"""Physical topology for the Hamilton SMC assay example.

Based on the bravo SMC topology (``examples/smc_assay/topology.py``) with the
two Bravo liquid handlers swapped for Hamilton ML STARs:

  * ``bravo_96`` -> ``mlstar_1``  (DDR-1 zone)
  * ``bravo_384`` -> ``mlstar_2`` (DDR-3 zone)

The shared devices (3 DDR zones, 2 translator bridges, 8 stackers, 10 shakers,
9 parking pads, biotek washers, centrifuge, sealer, delidder, reader,
plate hotel, waste) match the bravo SMC topology. This bench additionally
carries 4 thermocyclers (available for a TruSeq library-prep workflow; the SMC
assay does not use them), so it is a superset of bravo SMC rather than a direct
replica.

Topology = devices + locations + transporters + multi-device pools; it is
independent of any particular workflow. ``build_topology()`` returns a fresh
``Topology`` on every call so successive runs never share device or driver
state.

Devices use the no-driver SDK constructor; the active device factory (bound via
``use_device_factory(...)``) supplies the (live, sim) driver pair. This module
binds a small ``_SmcDeckFactory`` so both ML STARs fall back to
``ChatterboxLiquidHandlerDriver`` (a PLR-backed sim that tracks tips, well
volumes, and deck-resource placement) when no other factory is in scope -- which
lets the example run standalone in orca-core sim. When deployed through a hosted deployment the
``RemoteDeviceFactory`` is already bound, so this default is bypassed and the two
ML STARs dispatch to orca-client over the WebSocket. Tests that need to inspect
the pushed calls bind their own factory first (one returning a
``RecordingLiquidHandlerDriver`` per ML STAR); the outer binding wins.
"""

from contextlib import nullcontext

from cheshire_drivers import (
    AccessConfig,
    CartesianCoordinates,
    DeckLayoutConfig,
    DeckResourceConfig,
    Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver

from orca.devices.centrifuge import Centrifuge
from orca.devices.devices import Delidder, LiquidHandler, PlateWasher, Reader, Storage, Waste
from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from orca.devices.thermocycler import Thermocycler
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.translator import Translator
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_context import is_factory_bound, use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology


# Per-carrier handoffs: transporter drops at each carrier's handoff site, gripper
# relocates to the deck_positions= target on that carrier. Troughs are residents.
# Per-ML STAR PLR deck. Three Hamilton carriers per deck:
#   rail 7  -> PLT_CAR_L5AC_A00   working plates + handoff site
#   rail 15 -> TIP_CAR_480_A00    tip rack
#   rail 25 -> Trough_CAR_4R200_A00  deck-resident reagent reservoirs
# Child Locations (e.g. mlstar_1/carrier-25-0) are derived from each carrier's
# PLR site list. Labware lives in the workflow: transient labware enters via
# @orca.action(deck_positions=...); reservoirs anchor via
# @orca.thread(start=("<lh>/<carrier>-<site>", REUSE_EXISTING)).
MLSTAR_1_DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7",  catalog_ref="PLT_CAR_L5AC_A00",     rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00",      rail=15),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)

MLSTAR_2_DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7",  catalog_ref="PLT_CAR_L5AC_A00",     rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00",      rail=15),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
    ],
)


class _SmcDeckFactory:
    """Default factory for this example: a fresh ChatterboxLH per ML STAR, sim
    drivers for everything else. The two bridge translators get their
    one-carriage driver from being declared `Translator`, not from this factory.

    Honors any outer factory already bound: when a test wraps the topology build
    in its own ``use_device_factory(...)`` (e.g. to inject a per-ML STAR
    ``RecordingLiquidHandlerDriver``), the outer factory wins and this one is
    bypassed. A deployment whose ``RemoteDeviceFactory`` is bound likewise
    bypass this default.
    """

    def __init__(self) -> None:
        self._fallback = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if device_type == "liquid_handler":
            driver = ChatterboxLiquidHandlerDriver(num_channels=8)
            return driver, driver
        return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    """Build a fresh Hamilton SMC assay topology (bravo replica with ML STARs).

    Layout: 3 DDR robot zones connected by 2 translator bridges.

      DDR-1 (zone 1): biotek_1, mlstar_1, waste_1, pads 1-3
        |
      translator_1
        |
      DDR-2 (zone 2): centrifuge, sealer, delidder, stackers 1-8, shakers 1-10, thermocyclers 1-4, pads 4-6
        |
      translator_2
        |
      DDR-3 (zone 3): biotek_2, mlstar_2, plate_hotel, smc_pro, waste_2, pads 7-9

    Only bind the local default factory if no outer factory is in scope, so a
    test's recording factory (or a hosted deployment's remote factory) is not hidden by a
    nested binding.
    """
    ctx = nullcontext() if is_factory_bound() else use_device_factory(_SmcDeckFactory())

    with ctx:
        c = CartesianCoordinates

        default_vertical = AccessConfig(name="default_vertical", access_type="vertical")
        default_horizontal = AccessConfig(name="default_horizontal", access_type="horizontal")
        stores.access_configs(seed=[default_vertical, default_horizontal])

        ddr_1_teachpoints = [
            Teachpoint("biotek_1",            c(100, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("mlstar_1/carrier-7-2",  c(300, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("mlstar_1/carrier-15-1", c(320, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("translator_1_start",  c(450, 200, 300, 0, 90, 180), orientation="right", access=default_horizontal),
            Teachpoint("waste_1",             c(100, 400, 200, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("pad_1",               c(200, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("pad_2",               c(200, 300, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("pad_3",               c(400, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
        ]

        ddr_2_teachpoints = [
            Teachpoint("centrifuge",          c(600, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("sealer",              c(700, 200, 300, 0, 90, 180), orientation="right", access=default_horizontal),
            Teachpoint("delidder",            c(1350, 100, 300, 0, 90, 180), orientation="right", access=default_vertical),
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
            Teachpoint("thermocycler_1",      c(1300, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("thermocycler_2",      c(1400, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("thermocycler_3",      c(1500, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("thermocycler_4",      c(1600, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
        ]

        ddr_3_teachpoints = [
            Teachpoint("biotek_2",            c(1900, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("mlstar_2/carrier-7-2",  c(2100, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("mlstar_2/carrier-15-1", c(2120, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("plate_hotel",         c(2300, 200, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("smc_pro",             c(2300, 400, 300, 0, 90, 180), orientation="right", access=default_vertical),
            Teachpoint("translator_2_end",    c(1900, 50,  300, 0, 90, 180), orientation="right", access=default_horizontal),
            Teachpoint("waste_2",             c(2400, 100, 200, 0, 90, 180), orientation="right", access=default_vertical),
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

        biotek_1 = PlateWasher("biotek_1")
        biotek_2 = PlateWasher("biotek_2")
        mlstar_1 = LiquidHandler(
            "mlstar_1",
            deck_layout_store=stores.deck_layouts("mlstar_1", seed={"default": MLSTAR_1_DECK}),
            deck_layout="default",
        )
        mlstar_2 = LiquidHandler(
            "mlstar_2",
            deck_layout_store=stores.deck_layouts("mlstar_2", seed={"default": MLSTAR_2_DECK}),
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
        # A translator is ONE carriage on a rail: start/end are the same pad
        # at two positions.
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
            "mlstar_1": mlstar_1, "mlstar_2": mlstar_2,
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
