"""`orca start` runs the daemon in the caller's directory, and that folder is its package.

With no `ORCA_PACKAGE_DIR`, the daemon builds the folder it started in. A `cwd=`
on the spawn, or a daemon that changed directory at startup, would build the
wrong folder.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.cli import lifecycle
from orca.cli.app import app
from orca.cli.backend import _local_daemon_url
from orca.cli.daemon_client import DaemonLoadingClient
from tests.isolated_daemon import START_LIMIT_S
from orca.runtime.system_runtime import RuntimeState

runner = CliRunner()

_TOPOLOGY = """
from orca.resource_models.plate_pad import PlatePad
from orca.sdk.build import Topology


def build_topology():
    return Topology(locations={"pad_1": PlatePad("pad_1")}, transporters=[])
"""


@pytest.mark.timeout(START_LIMIT_S + 120.0)
def test_the_daemon_builds_the_folder_orca_start_ran_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "a_project_package"
    project.mkdir()
    (project / "topology.py").write_text(_TOPOLOGY, encoding="utf-8")
    monkeypatch.delenv("ORCA_PACKAGE_DIR")
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path / "daemon_home"))
    monkeypatch.chdir(project)
    # A cold start on a slow runner can outlast the CLI's own wait; this test is about where, not how fast.
    monkeypatch.setattr(lifecycle, "_HEALTH_POLL_TIMEOUT_S", START_LIMIT_S)

    try:
        started = runner.invoke(app, ["start"])
        assert started.exit_code == 0, started.output
        state = DaemonLoadingClient(_local_daemon_url()).mount_package()
        assert state == RuntimeState.RUNNING
    finally:
        runner.invoke(app, ["shutdown"])
