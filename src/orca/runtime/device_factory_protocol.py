"""Driver-provider Protocol used by `Device.__init__` for the no-driver SDK flow.

`IDeviceDriverProvider` is the narrow capability `Device` subclass
constructors consult when an author writes `Shaker(name="x")` with no
explicit `driver=` argument: just `build_drivers(device_type, name)`
returning a (live, sim) pair.

It lives in this small module so that `device_factory_context.py` and
Device subclasses can import the contract without pulling in the concrete
factory implementations (which sit downstream and would otherwise cycle).
`SimDeviceFactory` (orca-core) and the gateway's `RemoteDeviceFactory`
implement it.
"""

from typing import Protocol, Union

from cheshire_drivers.interfaces import BaseDriver, ITransporterDriver

DriverPairElement = Union[BaseDriver, ITransporterDriver]
"""Union covering both driver roots in cheshire-drivers.

Devices share `BaseDriver` (open/close/initialize/is_initialized).
`ITransporterDriver` is a separate root because its `initialize` takes
a typed Request whereas `BaseDriver.initialize()` is parameterless --
the two contracts are LSP-incompatible, so a single base class would
break the device hierarchy. `resolve_drivers` casts the union members
back to the concrete TDriver expected by each Device subclass.
"""


class IDeviceDriverProvider(Protocol):
    """Contract: build the (live, sim) driver pair for a Device subclass.

    See `orca.runtime.device_factory_context.resolve_drivers` for the
    flow that consults a bound provider.
    """

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        """Return the (live, sim) driver pair for the given device type + name.

        Args:
            device_type: One of the orca-core device-type strings
                (`shaker`, `centrifuge`, `transporter`, ...).
            name: Device identifier; the driver is wired with this name.
            deck_modeling: True when the requesting device models a deck
                (a ``LiquidHandler`` with addressable carrier sites). Only
                the ``liquid_handler`` device type reads it: it selects a
                deck-modeling sim slot (real Chatterbox deck) over the no-op
                protocol sim so PURE_SIM exercises a real deck. Every other
                device type ignores it.

        Returns:
            Tuple of (live_driver, sim_driver). `SimulationManager`
            toggles between them per dispatch. The element type is the
            union of both driver roots (`BaseDriver` for devices,
            `ITransporterDriver` for transporters); `resolve_drivers`
            narrows back to the concrete TDriver per Device subclass.

        Raises:
            ValueError: If the provider has no mapping for `device_type`.
        """
        ...
