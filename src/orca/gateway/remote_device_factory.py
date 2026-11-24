"""`IDeviceDriverProvider` that gives every device one driver, which forwards each command to its orca-client.

A deployment author writes `Shaker(name="ml_star")`; with this factory bound through
`use_device_factory(...)`, the constructor gets a `Remote*Driver` that sends every command
through the controller. orca-client runs it on the real driver or the device's simulator,
from the `effective_mode` the driver stamps. A check run binds a
factory over its own controller, whose orca-client runs inside Orca.

`mode_resolver` is required, because the mode it answers decides whether a command reaches
an instrument. It is a provider closure (`system_mode_resolver`), so it reads the current
topology after a rebuild, and drivers ask it on every dispatch.
"""

from typing import Callable, Optional, Protocol

from orca.gateway.controller.controller import IDeviceController
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
    RemoteStorageDriver,
    RemoteThermocyclerDriver,
    RemoteWasteDriver,
)
from orca.gateway.remote_transporter_driver import RemoteTranslatorDriver, RemoteTransporterDriver
from orca.driver_management.device_factory_protocol import DeviceDriver
from orca.runtime.registries.device_link import mark_agent_held

class _ForwardingDriverClass(Protocol):
    def __call__(
        self, name: str, controller: IDeviceController, mode_resolver: ModeResolver,
        *, declared_interfaces: frozenset[str] | None = None,
    ) -> DeviceDriver: ...


_REMOTE_DRIVER_BY_KIND: dict[str, _ForwardingDriverClass] = {
    "shaker": RemoteShakerDriver,
    "centrifuge": RemoteCentrifugeDriver,
    "thermocycler": RemoteThermocyclerDriver,
    "sealer": RemoteSealerDriver,
    "reader": RemoteReaderDriver,
    "delidder": RemoteDelidderDriver,
    "plate_washer": RemotePlateWasherDriver,
    "storage": RemoteStorageDriver,
    "waste": RemoteWasteDriver,
    "transporter": RemoteTransporterDriver,
    "translator": RemoteTranslatorDriver,
}


class RemoteDeviceFactory:
    """Builds each device's forwarding driver.

    `profile_source` maps (kind, name, deck_modeling) to the interfaces the device's setup says its
    driver implements. A liquid handler's class is picked from it (LH only, protocol only, or both).
    """

    def __init__(
        self,
        controller: IDeviceController,
        mode_resolver: ModeResolver,
        profile_source: Optional[Callable[[str, str, bool], frozenset[str]]] = None,
    ) -> None:
        self._controller = controller
        self._profile_source = profile_source
        self._mode_resolver = mode_resolver

    def _declared_interfaces(self, kind: str, name: str, deck_modeling: bool = False) -> Optional[frozenset[str]]:
        if self._profile_source is None:
            return None
        return self._profile_source(kind, name, deck_modeling)

    def build_driver(self, device_type: str, name: str, *, deck_modeling: bool = False) -> DeviceDriver:
        """The device's forwarding driver, stamped as reaching its instrument through orca-client."""
        declared = self._declared_interfaces(device_type, name, deck_modeling)
        cls = _liquid_handler_class(declared) if device_type == "liquid_handler" else _REMOTE_DRIVER_BY_KIND.get(device_type)
        if cls is None:
            supported = sorted([*_REMOTE_DRIVER_BY_KIND, "liquid_handler"])
            raise ValueError(f"Unknown device type {device_type!r}. Supported: {', '.join(supported)}")
        driver = cls(
            name=name,
            controller=self._controller,
            mode_resolver=self._mode_resolver,
            declared_interfaces=declared,
        )
        return mark_agent_held(driver)


def _liquid_handler_class(declared: Optional[frozenset[str]]) -> _ForwardingDriverClass:
    """The class whose `run_protocol` matches the setup: refused without IProtocolRunner, forwarded with it."""
    has_lh = declared is not None and "ILiquidHandler" in declared
    has_protocol = declared is not None and "IProtocolRunner" in declared
    if has_lh and has_protocol:
        return RemoteLiquidHandlerWithProtocolDriver
    if has_protocol:
        return RemoteProtocolOnlyLiquidHandlerDriver
    return RemoteLiquidHandlerDriver


__all__ = ["RemoteDeviceFactory"]
