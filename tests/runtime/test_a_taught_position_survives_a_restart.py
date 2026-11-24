"""What is taught is in the package's files, so a new process reads it back."""

import json
from pathlib import Path

from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.runtime.startup import PackageSetupStoreFactory
from cheshire_drivers.device_setup import BUILTIN_CLIENT


async def test_a_taught_position_survives_a_restart_and_is_in_the_file(tmp_path: Path) -> None:
    before = PackageSetupStoreFactory(tmp_path, default_client=BUILTIN_CLIENT)
    vertical = AccessConfig(name="vertical", access_type="vertical")
    await before.access_configs().add(vertical)
    await before.teachpoints("arm_1").add(Teachpoint(
        "pad_1", CartesianCoordinates(1, 2, 3, 0, 90, 180), orientation="right", access=vertical,
    ))

    after = PackageSetupStoreFactory(tmp_path, default_client=BUILTIN_CLIENT)
    read = await after.teachpoints("arm_1").get("pad_1")

    assert read is not None
    assert read.coordinates == CartesianCoordinates(1, 2, 3, 0, 90, 180)
    on_disk = json.loads((tmp_path / "teachpoints" / "arm_1.json").read_text(encoding="utf-8"))
    assert on_disk["pad_1"]["access"] == "vertical"
