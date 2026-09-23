"""The base every driver shares when its device is reached over the gateway.

Its one job is to be nameable. A gateway-built driver learns what its device
advertises from the connection card, and learns nothing at all when the runtime
was built before that device's on-prem client connected. Telling those two
apart from a driver that runs in-process decides whether the driver's class
default may narrow what the device is allowed to run, so the answer has to be
an `isinstance` against this class rather than a look for the attribute: a
local driver that grew an attribute of the same name would otherwise widen a
dispatch gate with nothing going red.
"""


class GatewayBackedDriver:
    """A driver whose device answers over the gateway rather than in-process."""

    def __init__(self, declared_interfaces: frozenset[str] | None = None) -> None:
        self._declared_interfaces = declared_interfaces

    @property
    def declared_interfaces(self) -> frozenset[str] | None:
        """What this device advertised, or None when no client had connected yet.

        Taken from the connection card when the runtime was built. The profile
        ClassVar that stands in for it is narrower than any real device, so
        None must not be read as an empty declaration.
        """
        return self._declared_interfaces
