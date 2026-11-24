"""`orca runtime reload` rebuilds a running daemon's package folder from the files on disk now."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.cli import lifecycle
from orca.cli.app import app
from orca.cli.backend import _local_daemon_url
from orca.cli.daemon_client import DaemonLoadingClient
from tests.isolated_daemon import START_LIMIT_S

runner = CliRunner()

_TOPOLOGY = """
from orca.resource_models.plate_pad import PlatePad
from orca.sdk.build import Topology


def build_topology():
    return Topology(locations={"pad_1": PlatePad("pad_1")}, transporters=[])
"""

_WORKFLOW = """
from cheshire_drivers.plr.plates import Cor_Falcon_96_wellplate_340ul_Fb_Black

import orca.orca as orca
from orca.sdk.labware import PlateTemplate

plate = PlateTemplate("plate", Cor_Falcon_96_wellplate_340ul_Fb_Black)


def build_workflow(topology):
    @orca.method
    async def wait(ctx):
        return
        yield

    @orca.thread(labware=plate, start="pad_1", end="pad_1")
    async def journey(ctx):
        yield wait

    @orca.workflow(name="added_since_the_mount")
    def workflow(wf):
        wf.start(journey)

    return workflow
"""


@pytest.mark.timeout(START_LIMIT_S + 120.0)
def test_orca_runtime_reload_builds_a_workflow_added_since_the_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "a_reloaded_package"
    project.mkdir()
    (project / "topology.py").write_text(_TOPOLOGY, encoding="utf-8")
    monkeypatch.setenv("ORCA_PACKAGE_DIR", str(project))
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path / "daemon_home"))
    monkeypatch.setattr(lifecycle, "_HEALTH_POLL_TIMEOUT_S", START_LIMIT_S)

    try:
        started = runner.invoke(app, ["start"])
        assert started.exit_code == 0, started.output
        DaemonLoadingClient(_local_daemon_url()).mount_package()
        (project / "workflows").mkdir()
        (project / "workflows" / "added_since_the_mount.py").write_text(_WORKFLOW, encoding="utf-8")

        reloaded = runner.invoke(app, ["runtime", "reload", "--reason", "added a workflow"])

        assert reloaded.exit_code == 0, reloaded.output
        assert "added_since_the_mount" in reloaded.output
    finally:
        runner.invoke(app, ["shutdown"])
