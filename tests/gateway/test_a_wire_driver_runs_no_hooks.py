"""A driver that forwards over the gateway runs no hooks: orca-client's driver runs them, once."""

import inspect

from cheshire_drivers.driver_hooks import DriverHooks, is_hooked

from orca.gateway import remote_drivers, remote_transporter_driver
from orca.gateway.gateway_backed_driver import GatewayBackedDriver


def _wire_driver_classes() -> list[type[GatewayBackedDriver]]:
    found: list[type[GatewayBackedDriver]] = []
    for module in (remote_drivers, remote_transporter_driver):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, GatewayBackedDriver) and issubclass(cls, DriverHooks) and cls not in found:
                found.append(cls)
    return found


def test_the_walk_finds_the_wire_drivers() -> None:
    names = {cls.__name__ for cls in _wire_driver_classes()}

    assert {"RemoteShakerDriver", "RemoteTransporterDriver", "RemoteLiquidHandlerDriver"} <= names


def test_no_wire_driver_command_is_wrapped_by_the_hook() -> None:
    wrapped = [
        f"{cls.__name__}.{name}"
        for cls in _wire_driver_classes()
        for name, member in inspect.getmembers(cls, inspect.iscoroutinefunction)
        if not cls.runs_hooks and is_hooked(member) and name in vars(cls)
    ]
    opted_in = [cls.__name__ for cls in _wire_driver_classes() if cls.runs_hooks]

    assert opted_in == []
    assert wrapped == []
