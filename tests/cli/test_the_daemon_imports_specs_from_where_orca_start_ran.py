"""`orca start` runs the daemon in the caller's directory, so a spec can name a module there.

The daemon, not the CLI, imports `module:build_topology`. A `cwd=` on the spawn,
or a daemon that changed directory at startup, would break every project-local spec.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.cli import lifecycle
from orca.cli.app import app
from orca.cli.client import LocalDaemonClient
from orca.runtime.system_runtime import RuntimeState

runner = CliRunner()

_TOPOLOGY = """
from orca.resource_models.plate_pad import PlatePad
from orca.sdk.build import Topology


def build_topology(stores):
    return Topology(locations={"pad_1": PlatePad("pad_1")}, transporters=[])
"""


@pytest.mark.timeout(120)
def test_a_spec_resolves_from_the_directory_orca_start_ran_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "a_project_local_topology.py").write_text(_TOPOLOGY, encoding="utf-8")
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path / "daemon_home"))
    monkeypatch.chdir(project)
    # A cold start on a slow runner can outlast the CLI's own wait; this test is about where, not how fast.
    monkeypatch.setattr(lifecycle, "_HEALTH_POLL_TIMEOUT_S", 60.0)

    try:
        started = runner.invoke(app, ["start"])
        assert started.exit_code == 0, started.output
        state = LocalDaemonClient().mount_topology(
            "a_project_local_topology:build_topology", sim=True,
        )
        assert state == RuntimeState.RUNNING
    finally:
        runner.invoke(app, ["shutdown"])
