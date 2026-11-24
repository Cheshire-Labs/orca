"""The tests' real device path stands in for a sim or live device, so it expects the real driver's interfaces.

It runs every mode on the check run's in-process orca-client, but a check run expects only the simulator's
interfaces. The device a test builds must expect what the server expects of the same setup on a lab PC.
"""

from cheshire_drivers.device_setup import DeviceSetup, RealDriverSetup, TcpConnection
from cheshire_drivers.driver_lookup import driver_interfaces, sim_driver_interfaces

from orca.devices.devices import LiquidHandlerProtocol
from orca.driver_management.device_factory_protocol import DeviceDriver
from orca.gateway.gateway_backed_driver import GatewayBackedDriver
from orca.runtime.server_events import ServerEvents
from orca.runtime.startup import build_check_run_devices
from tests.real_device_path import the_real_device_path


def _declared(driver: DeviceDriver) -> frozenset[str] | None:
    assert isinstance(driver, GatewayBackedDriver), driver
    return driver.declared_interfaces


_OT2 = DeviceSetup(
    client="lab_pc",
    real=RealDriverSetup(type="plr", backend="OpentronsOT2Backend", connection=TcpConnection(type="tcp", host="ot2")),
)


def test_a_device_on_the_real_device_path_expects_the_real_drivers_interfaces() -> None:
    the_real_device_path().set_up("lh1", _OT2)

    declared = _declared(LiquidHandlerProtocol("lh1").driver)

    assert declared == driver_interfaces("liquid_handler", _OT2)
    assert declared is not None and "IProtocolRunner" not in declared


def test_a_check_runs_device_expects_its_simulators_interfaces() -> None:
    path = the_real_device_path()
    path.set_up("lh1", _OT2)
    check_run = build_check_run_devices(path.stores, lambda: None, ServerEvents())

    declared = _declared(check_run.device_factory.build_driver("liquid_handler", "lh1"))

    assert declared == sim_driver_interfaces("liquid_handler", _OT2)
    assert declared is not None and "IPipetteMotion" in declared
