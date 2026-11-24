"""A mount builds when a saved plate sits where a transporter reaches, with no orca-client connected.

The build restores the plate to its slot and record only. It tells no device: the transporter's
world is rebuilt from the record before its next command.
"""

import shutil
from pathlib import Path

from orca.daemon.app import create_app
from orca.resource_models.labware import LabwareInstance
from tests.daemon.started_daemon import started_daemon

_PACKAGE = Path(__file__).resolve().parents[2] / "examples" / "multi_lineage"


async def test_a_mount_restores_a_saved_plate_a_transporter_reaches_without_telling_the_transporter(
    tmp_path: Path,
) -> None:
    package = tmp_path / "multi_lineage"
    shutil.copytree(_PACKAGE, package)
    app = create_app(package_root=package)
    saved = LabwareInstance("sample", "Cor_Falcon_96_wellplate_340ul_Fb_Black")

    async with started_daemon(app) as client:
        activity = app.state.daemon.activity
        await activity.labware.register(saved)
        await activity.labware.update_location(saved.id, "start_pad")

        mounted = await client.post("/api/runtime/mount-package", json={})

        assert mounted.status_code == 200, mounted.text
        runtime = app.state.daemon.holder.runtime
        start_pad = runtime.system.system_map.get_location("start_pad")
        assert start_pad.labware is not None and start_pad.labware.id == saved.id
        (await client.post("/api/runtime/unload", json={})).raise_for_status()
