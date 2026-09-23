"""`IDeviceDriverProvider` impl that wires remote-forwarding drivers.

`RemoteDeviceFactory.build_drivers(device_type, name)` returns the (live, sim)
driver pair for a device type:
  * live = a `Remote*Driver` from `remote_drivers.py` that forwards every
    method over `DeviceController.execute_command`.
  * sim = a cheshire-drivers `Sim*Driver` running in-process.

The orca-core `SimulationManager` decides per dispatch which of the two to
actually call, based on the per-execution mode resolution. When the live
(`Remote*Driver`) path is taken, this driver pulls the per-device
`effective_mode` from a factory-supplied resolver and forwards it on the wire
so the device bridge can pick the matching backend (real vs Sim*).

This factory uses the no-driver SDK pattern: deployment authors write
`Shaker(name="ml_star")` and the orca-core `Device` ctor consults
`use_device_factory(...)` for `(live, sim)` driver pairs via
`IDeviceDriverProvider.build_drivers`. `RuntimeLifecycle` types its
`device_factory` parameter as `IDeviceDriverProvider`; standalone
(non-gateway) deployments instead bind orca-core's `SimDeviceFactory`.

Mode resolver
-------------
`mode_resolver` is required: a factory with nobody to ask cannot guess,
because the guess decides whether a command reaches an instrument. The
resolver is a provider closure (`orca.gateway.mode_resolution.system_mode_resolver`)
so it can be built before the runtime exists and still read the current
topology afterwards. Drivers do not cache the mode; they call the
resolver on every dispatch.
"""

import logging
from typing import Callable, Dict, Optional

from cheshire_drivers.interfaces import (
    ICentrifugeDriver,
    IDelidderDriver,
    ILiquidHandlerDriver,
    IPlateWasherDriver,
    IReaderDriver,
    ISealerDriver,
    IShakerDriver,
    IStorageDriver,
    IThermocyclerDriver,
    ITransporterDriver,
    IWasteDriver,
)
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
from orca.runtime.registries.device_link import mark_agent_held
from orca.runtime.run_modes import WorkflowRunMode

from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_drivers import (
    ModeResolver,
    RemoteCentrifugeDriver,
    RemoteDelidderDriver,
    RemoteLiquidHandlerDriver,
    RemoteLiquidHandlerWithProtocolDriver,
    RemotePlateWasherDriver,
    RemoteProtocolOnlyLiquidHandlerDriver,
    RemoteReaderDriver,
    RemoteSealerDriver,
    RemoteShakerDriver,
    RemoteThermocyclerDriver,
)
from orca.gateway.remote_transporter_driver import (
    RemoteTranslatorDriver,
    RemoteTransporterDriver,
)

logger = logging.getLogger(__name__)


class RemoteDeviceFactory:
    """Creates orca-core devices wired with `Remote*Driver` plus paired sims.

    Each device carries BOTH drivers: the live wire-forwarding driver and
    an in-process cheshire-drivers Sim* driver. The orca-core
    `SimulationManager` toggles between them per dispatch.

    Construction-time inputs:
      * `controller`: shared `DeviceController` for command execution.
      * `default_timeout`: per-command WebSocket timeout (seconds).
      * `mode_resolver`: callable returning the per-device effective_mode
        at dispatch time. Required, and normally a provider closure over
        the runtime so a topology rebuild is picked up without rebuilding
        drivers.
      * `profile_source` (optional): callable that returns a device's
        advertised interface set (from the device-bridge connection card), or
        None when no client has advertised the device yet. Liquid handlers
        use it to pick their per-instance capability profile (plr-only /
        protocol-only / both). When omitted, every device falls back to the
        cold-start default (plr-only for liquid handlers).
    """

    def __init__(
        self,
        controller: DeviceController,
        mode_resolver: ModeResolver,
        default_timeout: float = 30.0,
        profile_source: Optional[Callable[[str], Optional[frozenset[str]]]] = None,
    ) -> None:
        self._controller = controller
        self._default_timeout = default_timeout
        self._profile_source = profile_source
        self._mode_resolver = mode_resolver

    def _resolve(self, device_name: str) -> WorkflowRunMode:
        """Ask the bound resolver which world this device is in."""
        return self._mode_resolver(device_name)

    def _advertised_interfaces(self, device_name: str) -> Optional[frozenset[str]]:
        """The device's advertised interface set, or None when unknown.

        None means no device bridge has advertised this device yet (cold start);
        callers fall back to a safe default profile.
        """
        if self._profile_source is None:
            return None
        return self._profile_source(device_name)

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        """Return the (Remote*Driver, Sim*Driver) pair for a device type.

        Used by the no-driver SDK constructor flow: when the deployment
        package author writes ``Shaker(name="ml_star")`` and the hosting
        layer has bound this factory via ``use_device_factory(...)``, the
        Shaker constructor calls into this method to obtain its drivers.

        ``deck_modeling`` is read only for ``liquid_handler``: a
        deck-modeling ``LiquidHandler`` gets the Chatterbox sim slot so
        PURE_SIM exercises a real deck; every other device type ignores it.

        Every live slot leaves here stamped as agent-held, because being built
        by this factory is exactly what makes that true. Read surfaces refuse
        to answer a device's link off such a driver: for a device with a wire
        surface it is a proxy holding a cache, and for a passive one it is a
        local simulator that was never asked about any instrument.
        """
        if device_type == "liquid_handler":
            live, sim = _drivers_for_liquid_handler(
                self, name, deck_modeling=deck_modeling,
            )
            return mark_agent_held(live), sim
        driver_builder = _DRIVER_BUILDERS.get(device_type)
        if driver_builder is None:
            raise ValueError(
                f"Unknown device type {device_type!r}. "
                f"Supported: {', '.join(sorted(_DRIVER_BUILDERS.keys()))}"
            )
        live, sim = driver_builder(self, name)
        return mark_agent_held(live), sim

# Device-type-keyed driver builders: single source of truth for the
# (Remote*Driver, Sim*Driver) pairing, consumed by `build_drivers`.


def _drivers_for_shaker(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[IShakerDriver, IShakerDriver]:
    live = RemoteShakerDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimShakerDriver(name)


def _drivers_for_centrifuge(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[ICentrifugeDriver, ICentrifugeDriver]:
    live = RemoteCentrifugeDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimCentrifugeDriver(name)


def _drivers_for_thermocycler(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[IThermocyclerDriver, IThermocyclerDriver]:
    live = RemoteThermocyclerDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimThermocyclerDriver(name)


def _drivers_for_sealer(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[ISealerDriver, ISealerDriver]:
    live = RemoteSealerDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimSealerDriver(name)


def _drivers_for_reader(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[IReaderDriver, IReaderDriver]:
    live = RemoteReaderDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimReaderDriver(name)


def _drivers_for_delidder(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[IDelidderDriver, IDelidderDriver]:
    live = RemoteDelidderDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimDelidderDriver(name)


def _drivers_for_plate_washer(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[IPlateWasherDriver, IPlateWasherDriver]:
    live = RemotePlateWasherDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimPlateWasherDriver(name)


def _drivers_for_liquid_handler(
    factory: "RemoteDeviceFactory", name: str, *, deck_modeling: bool = False,
) -> tuple[ILiquidHandlerDriver, ILiquidHandlerDriver]:
    """Select the LH profile from the device's advertised interface set.

    The profile source is the device bridge's `DeviceConnectInfo.interfaces`
    surfaced through the connection tracker. The coarse class picks run_protocol
    behavior (plr-only fails fast; protocol / both forward), while the full
    advertised card rides on the driver's per-instance `declared_interfaces` so
    facets one class cannot enumerate (a Flex's motion interfaces) reach the
    topology card via the registry and survive declared-INTERSECT-advertised.
    Cold start (no card yet) passes None: the class stays plr-only (never
    over-promises) and the registry falls back to its ClassVar until a rebuild
    after the client connects upgrades it.

    The sim slot depends on whether the device models a deck. A deck-modeling
    `LiquidHandler` gets the deck-modeling composite
    `ChatterboxLiquidHandlerWithProtocolDriver` (real Chatterbox deck,
    `provides_state=True`) so PURE_SIM exercises occupancy/move/reconcile
    rather than a no-op. A deckless `LiquidHandlerProtocol` keeps the no-op
    composite `SimLiquidHandlerWithProtocolDriver`: it has no deck on live
    either, and the SMC reference drives such handlers via `run_protocol`.
    Either sim legitimately advertises a superset of the live driver: the
    operator surface + connect check read the LIVE driver's interfaces, not
    the sim's, so the broader sim does not over-promise the device.
    """
    advertised = factory._advertised_interfaces(name)
    has_lh = advertised is not None and "ILiquidHandler" in advertised
    has_protocol = advertised is not None and "IProtocolRunner" in advertised

    if has_lh and has_protocol:
        live: ILiquidHandlerDriver = RemoteLiquidHandlerWithProtocolDriver(
            name=name,
            controller=factory._controller,
            mode_resolver=factory._resolve,
            timeout=factory._default_timeout,
            declared_interfaces=advertised,
        )
    elif has_protocol:
        live = RemoteProtocolOnlyLiquidHandlerDriver(
            name=name,
            controller=factory._controller,
            mode_resolver=factory._resolve,
            timeout=factory._default_timeout,
            declared_interfaces=advertised,
        )
    else:
        live = RemoteLiquidHandlerDriver(
            name=name,
            controller=factory._controller,
            mode_resolver=factory._resolve,
            timeout=factory._default_timeout,
            declared_interfaces=advertised,
        )
    if deck_modeling:
        from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver

        return live, ChatterboxLiquidHandlerWithProtocolDriver()
    return live, SimLiquidHandlerWithProtocolDriver(name)


def _drivers_for_transporter(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[ITransporterDriver, ITransporterDriver]:
    """Return the (Remote*Driver, Sim*Driver) pair for a transporter.

    Mirrors `_drivers_for_shaker` etc. The live driver forwards every
    method over `DeviceController.execute_command`; the sim driver is
    in-process. orca-core's `SimulationManager` toggles between them
    per dispatch via the factory's mode resolver.

    Resolution + gateway-walk is now upstream in orca-core's
    `Transporter.pick`/`.place`, so the wire payload arriving at the
    driver carries an already-resolved Teachpoint plus pre-walked
    gateway path; the driver does not consult any teachpoint store.
    """
    live = RemoteTransporterDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimTransporterDriver(name)


def _drivers_for_translator(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[ITransporterDriver, ITransporterDriver]:
    """Return the (RemoteTranslatorDriver, SimTranslatorDriver) pair.

    Same wire surface as a transporter; both slots declare the one-carriage
    fact the reservation layer reads at build time, so a hosted deployment
    models the bridge the same way a standalone one does.
    """
    live = RemoteTranslatorDriver(
        name=name,
        controller=factory._controller,
        mode_resolver=factory._resolve,
        timeout=factory._default_timeout,
        declared_interfaces=factory._advertised_interfaces(name),
    )
    return live, SimTranslatorDriver(name)


def _drivers_for_storage(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[IStorageDriver, IStorageDriver]:
    """Return ``(sim, sim)`` for a storage device.

    ``IStorageDriver`` declares no methods (it's a passive resource: the
    workflow only stages/loads labware on its locations, never dispatches
    a command at the driver). There is no wire surface to forward, so
    we don't need a Remote*Driver. Both slots use the same
    ``SimStorageDriver``; the SimulationManager toggle is meaningless.
    """
    sim = SimStorageDriver(name)
    return sim, sim


def _drivers_for_waste(
    factory: "RemoteDeviceFactory", name: str,
) -> tuple[IWasteDriver, IWasteDriver]:
    """Return ``(sim, sim)`` for a waste device. See ``_drivers_for_storage``."""
    sim = SimWasteDriver(name)
    return sim, sim


_DRIVER_BUILDERS: Dict[
    str, Callable[["RemoteDeviceFactory", str], tuple[DriverPairElement, DriverPairElement]],
] = {
    "shaker": _drivers_for_shaker,
    "centrifuge": _drivers_for_centrifuge,
    "thermocycler": _drivers_for_thermocycler,
    "sealer": _drivers_for_sealer,
    "reader": _drivers_for_reader,
    "delidder": _drivers_for_delidder,
    "plate_washer": _drivers_for_plate_washer,
    "liquid_handler": _drivers_for_liquid_handler,
    "transporter": _drivers_for_transporter,
    "translator": _drivers_for_translator,
    "storage": _drivers_for_storage,
    "waste": _drivers_for_waste,
}


__all__ = ["RemoteDeviceFactory"]
