"""orca's centrifuge, reader, shaker and storage devices run commands on PyLabRobot's Chatterbox drivers.

For these four devices, the Chatterbox drivers are the only simulation that goes
through PyLabRobot, so these are the only tests that orca's device calls fit the
PLR wrappers in cheshire-drivers. The in-process Sim drivers never touch PyLabRobot.
"""

from pathlib import Path

from cheshire_drivers.plr import (
    ChatterboxCentrifugeDriver,
    ChatterboxReaderDriver,
    ChatterboxShakerDriver,
    ChatterboxStorageDriver,
)

from orca.devices.centrifuge import Centrifuge
from orca.devices.devices import Reader, Storage
from orca.devices.shaker import Shaker
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement


class _ChatterboxFactory:
    """Builds one Chatterbox driver per device, used for both the live and sim slot."""

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        driver: DriverPairElement
        if device_type == "centrifuge":
            driver = ChatterboxCentrifugeDriver()
        elif device_type == "reader":
            driver = ChatterboxReaderDriver()
        elif device_type == "shaker":
            driver = ChatterboxShakerDriver()
        elif device_type == "storage":
            driver = ChatterboxStorageDriver()
        else:
            raise AssertionError(f"no Chatterbox driver for {device_type!r}")
        return driver, driver


async def test_a_centrifuge_spins_on_its_chatterbox_driver() -> None:
    with use_device_factory(_ChatterboxFactory()):
        centrifuge = Centrifuge("centrifuge_1")
    assert isinstance(centrifuge.driver, ChatterboxCentrifugeDriver)

    await centrifuge.initialize()
    await centrifuge.centrifuge(g=500, duration=1)


async def test_a_reader_reads_on_its_chatterbox_driver(tmp_path: Path) -> None:
    with use_device_factory(_ChatterboxFactory()):
        reader = Reader("reader_1")
    assert isinstance(reader.driver, ChatterboxReaderDriver)

    await reader.initialize()
    await reader.read(str(tmp_path / "protocol.prt"), str(tmp_path / "output.csv"))


async def test_a_shaker_shakes_on_its_chatterbox_driver() -> None:
    with use_device_factory(_ChatterboxFactory()):
        shaker = Shaker("shaker_1")
    assert isinstance(shaker.driver, ChatterboxShakerDriver)

    await shaker.initialize()
    await shaker.shake(duration=1, speed=500)


async def test_a_storage_dispenses_on_its_chatterbox_driver() -> None:
    with use_device_factory(_ChatterboxFactory()):
        storage = Storage("stacker_1")
    assert isinstance(storage.driver, ChatterboxStorageDriver)

    await storage.initialize()
    await storage.dispense()
