"""The device-factory contract a Device subclass's constructor consults for its driver.

It lives apart from the factories so `device_factory_context.py` and the Device subclasses
can import it without importing the factories, which would cycle.
"""

from typing import Protocol, Union

from cheshire_drivers.interfaces import BaseDriver, ITransporterDriver

# Two roots: `ITransporterDriver.initialize` takes a request and `BaseDriver.initialize` does not.
DeviceDriver = Union[BaseDriver, ITransporterDriver]


class IDeviceDriverProvider(Protocol):
    def build_driver(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> DeviceDriver:
        """The one driver for device `name` of kind `device_type`; ValueError for a kind it cannot build.

        `deck_modeling` is True for a liquid handler that keeps a model of its deck.
        """
        ...
