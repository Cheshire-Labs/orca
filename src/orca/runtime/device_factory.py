"""Simulation device-driver provider for the no-driver SDK constructor flow.

`SimDeviceFactory` is orca-core's source-available default `IDeviceDriverProvider`:
every device type resolves to a pair of cheshire-drivers sim drivers. A
deployment-package author writes `Shaker(name="x")` with no driver argument and,
when this factory is bound via `use_device_factory(...)`, the Device `__init__`
flow calls `build_drivers(device_type, name)` to obtain its (live, sim) pair.
Used by tests and standalone pure-sim deployments.
"""

from cheshire_drivers.sims import (
    SimCentrifugeDriver,
    SimDelidderDriver,
    SimLiquidHandlerWithProtocolDriver,
    SimPlateWasherDriver,
    SimReaderDriver,
    SimSealerDriver,
    SimShakerDriver,
    SimStorageDriver,
    SimThermocyclerDriver,
    SimTransporterDriver,
    SimWasteDriver,
)
from cheshire_drivers.translator_driver import SimTranslatorDriver

from orca.runtime.device_factory_protocol import DriverPairElement


_SIM_DRIVER_MAP: dict[str, type] = {
    "shaker": SimShakerDriver,
    "centrifuge": SimCentrifugeDriver,
    "thermocycler": SimThermocyclerDriver,
    "sealer": SimSealerDriver,
    "delidder": SimDelidderDriver,
    "reader": SimReaderDriver,
    "plate_washer": SimPlateWasherDriver,
    "liquid_handler": SimLiquidHandlerWithProtocolDriver,
    "storage": SimStorageDriver,
    "waste": SimWasteDriver,
    "transporter": SimTransporterDriver,
    "translator": SimTranslatorDriver,
}


class SimDeviceFactory:
    """Sim `IDeviceDriverProvider`: every device type gets sim drivers in both slots.

    Bound via `use_device_factory(...)` so each `Shaker(name="x")` style
    constructor resolves its (live, sim) pair through `build_drivers`. Used
    for testing, development, and standalone pure-sim deployments.
    """

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if device_type == "liquid_handler" and deck_modeling:
            # Deck-modeling LH gets a real Chatterbox deck in both slots so
            # PURE_SIM exercises occupancy/move/reconcile, not the no-op sim.
            from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver

            return (
                ChatterboxLiquidHandlerWithProtocolDriver(),
                ChatterboxLiquidHandlerWithProtocolDriver(),
            )
        sim_driver_cls = _SIM_DRIVER_MAP.get(device_type)
        if sim_driver_cls is None:
            raise ValueError(
                f"Unknown device type '{device_type}'. "
                f"Supported: {', '.join(sorted(_SIM_DRIVER_MAP.keys()))}"
            )
        # Two distinct instances so SimulationManager keeps independent state
        # per slot (real factories return a distinct live/sim pair).
        return sim_driver_cls(name), sim_driver_cls(name)
