"""Active device factory binding for the no-driver SDK constructor flow.

The deployment-package author writes::

    shaker = Shaker(name="ml_star")  # no driver argument

Each Device subclass's `__init__` asks the factory bound with `use_device_factory(factory)`
for its one driver. Both servers and a check run bind a factory whose drivers send every
command to an orca-client. Building a device with no factory bound
is an error.

The binding is a `ContextVar`, so concurrent topology builds in different tasks do not
interfere, and nested bindings restore in LIFO order.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, TypeVar

from orca.driver_management.device_factory_protocol import IDeviceDriverProvider

TDriver = TypeVar("TDriver")


class NoDeviceFactoryError(RuntimeError):
    """A device was built outside a server, which is what hands each device its driver."""


_active_factory: ContextVar[IDeviceDriverProvider | None] = ContextVar(
    "_active_device_factory", default=None,
)


@contextmanager
def use_device_factory(factory: IDeviceDriverProvider) -> Iterator[None]:
    """Bind `factory` as the active device factory for the duration of the block."""
    token = _active_factory.set(factory)
    try:
        yield
    finally:
        _active_factory.reset(token)


def resolve_driver(
    device_type: str,
    name: str,
    driver_type: type[TDriver],
    *,
    deck_modeling: bool = False,
) -> TDriver:
    """The device's one driver, from the bound factory. It must be a `driver_type`.

    `deck_modeling` is passed to the factory only when True, so a factory that never builds a
    deck-modeling liquid handler needs no such parameter.
    """
    factory = _active_factory.get()
    if factory is None:
        raise NoDeviceFactoryError(
            f"{device_type} {name!r} was built outside an Orca server, so it has no driver. "
            "Name each device in a package folder's topology.py and let the server build it: "
            "run `orca start` in that folder, then `orca topology mount`, "
            "then `orca run <workflow> --run-mode SIM`. "
            'The "Command line" section of the README shows the steps.'
        )
    if deck_modeling:
        driver = factory.build_driver(device_type, name, deck_modeling=True)
    else:
        driver = factory.build_driver(device_type, name)
    if not isinstance(driver, driver_type):
        raise TypeError(
            f"The device factory built a {type(driver).__name__} for {device_type} {name!r}, "
            f"which is not a {driver_type.__name__}."
        )
    return driver
