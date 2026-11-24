"""A device a package builds when its module is imported gets the server's driver, never a local simulator."""

import sys
import uuid
from pathlib import Path

import pytest
from cheshire_drivers.sims import SimShakerDriver

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.driver_management.device_factory_protocol import DeviceDriver
from tests.memory_activity import memory_labware_service
from tests.memory_activity import memory_unsaved_positions
from tests.memory_activity import memory_ops_store
from tests.setup_package import package_stores

_TOPOLOGY = '''
from orca.devices.shaker import Shaker
from orca.resource_models.plate_pad import PlatePad
from orca.sdk.build import Topology

shaker = Shaker("shaker_1")


def build_topology() -> Topology:
    return Topology(locations={"shaker_1": shaker, "pad": PlatePad("pad")}, transporters=[])
'''


class _ServersFactory:
    def __init__(self) -> None:
        self.built: list[DeviceDriver] = []

    def build_driver(self, device_type: str, name: str, *, deck_modeling: bool = False) -> DeviceDriver:
        driver = SimShakerDriver(name)
        self.built.append(driver)
        return driver


@pytest.mark.asyncio
async def test_a_device_built_when_topology_py_is_imported_takes_the_factorys_driver(tmp_path: Path) -> None:
    package = f"pkg_{uuid.uuid4().hex}"
    (tmp_path / package).mkdir()
    (tmp_path / package / "topology.py").write_text(_TOPOLOGY, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    factory = _ServersFactory()
    try:
        build = await orca.build_system_from_package(
            package, package_stores(tmp_path / package), memory_ops_store(), factory, event_bus=EventBus(),
            labware_service=memory_labware_service(),
            unsaved_positions=memory_unsaved_positions(),
        )
    finally:
        sys.path.remove(str(tmp_path))

    assert build.system.get_resource("shaker_1").driver in factory.built
