"""CLI dispatch tests for the split verbs `orca topology mount`
and `orca workflow load`.

The route + resolver layers are covered by tests/daemon/test_routes_mount.py
and tests/daemon/test_system_builder.py. These tests cover the CLI layer the
operator actually types: backend selection (local factory spec vs cloud source
file) and the cloud-branch usage guards (missing --name / --message).
"""

from unittest.mock import patch

from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.output import EXIT_USAGE
from orca.runtime.system_runtime import RuntimeState


runner = CliRunner()


# -- topology mount ----------------------------------------------------------


def test_topology_mount_local_calls_mount_topology() -> None:
    """Local backend forwards the factory spec to the daemon's mount endpoint."""
    with patch("orca.cli.topology.active_backend", return_value="local"), patch(
        "orca.cli.client.LocalDaemonClient"
    ) as mock_client:
        mock_client.return_value.mount_topology.return_value = RuntimeState.RUNNING
        result = runner.invoke(
            app, ["topology", "mount", "pkg.mod:build_topology", "--sim"],
        )
    assert result.exit_code == 0, result.output
    mock_client.return_value.mount_topology.assert_called_once_with(
        "pkg.mod:build_topology", sim=True,
    )
    assert "mounted topology" in result.output


def test_topology_mount_cloud_requires_existing_source_file() -> None:
    """Cloud backend treats the arg as a file path; a missing file is a usage error."""
    with patch("orca.cli.topology.active_backend", return_value="cloud"):
        result = runner.invoke(
            app,
            ["topology", "mount", "definitely_missing_topology.py", "-m", "msg"],
        )
    assert result.exit_code == EXIT_USAGE, result.output


def test_topology_mount_cloud_requires_message(tmp_path) -> None:
    """Cloud backend rejects a source file submission with no commit message."""
    src = tmp_path / "topology.py"
    src.write_text("# topology\n")
    with patch("orca.cli.topology.active_backend", return_value="cloud"):
        result = runner.invoke(app, ["topology", "mount", str(src)])
    assert result.exit_code == EXIT_USAGE, result.output
    assert "message" in result.output.lower()


def test_topology_mount_cloud_submits_source(tmp_path) -> None:
    """Cloud backend reads the file and submits it via the cloud client."""
    src = tmp_path / "topology.py"
    src.write_text("# topology\n")
    with patch("orca.cli.topology.active_backend", return_value="cloud"), patch(
        "orca.cli.topology.cloud_client"
    ) as mock_cloud:
        mock_cloud.return_value.topology_submit.return_value.commit_sha = "abc12345"
        result = runner.invoke(app, ["topology", "mount", str(src), "-m", "msg"])
    assert result.exit_code == 0, result.output
    mock_cloud.return_value.topology_submit.assert_called_once_with(
        "# topology\n", "msg",
    )


# -- workflow load -----------------------------------------------------------


def test_workflow_load_local_calls_load_workflow() -> None:
    """Local backend forwards the factory spec to the daemon's workflow endpoint."""
    with patch("orca.cli.backend.active_backend", return_value="local"), patch(
        "orca.cli.client.LocalDaemonClient"
    ) as mock_client:
        mock_client.return_value.load_workflow.return_value = "simple_workflow"
        result = runner.invoke(
            app, ["workflow", "load", "pkg.mod:build_workflow"],
        )
    assert result.exit_code == 0, result.output
    mock_client.return_value.load_workflow.assert_called_once_with(
        "pkg.mod:build_workflow",
    )
    assert "simple_workflow" in result.output


def test_workflow_load_cloud_requires_existing_source_file() -> None:
    """Cloud backend treats the arg as a file path; a missing file is a usage error."""
    with patch("orca.cli.backend.active_backend", return_value="cloud"):
        result = runner.invoke(
            app,
            ["workflow", "load", "missing_wf.py", "--name", "wf", "-m", "msg"],
        )
    assert result.exit_code == EXIT_USAGE, result.output


def test_workflow_load_cloud_requires_name(tmp_path) -> None:
    """Cloud backend rejects a submission with no --name."""
    src = tmp_path / "wf.py"
    src.write_text("# wf\n")
    with patch("orca.cli.backend.active_backend", return_value="cloud"):
        result = runner.invoke(app, ["workflow", "load", str(src), "-m", "msg"])
    assert result.exit_code == EXIT_USAGE, result.output
    assert "name" in result.output.lower()


def test_workflow_load_cloud_requires_message(tmp_path) -> None:
    """Cloud backend rejects a submission with no commit message."""
    src = tmp_path / "wf.py"
    src.write_text("# wf\n")
    with patch("orca.cli.backend.active_backend", return_value="cloud"):
        result = runner.invoke(app, ["workflow", "load", str(src), "--name", "wf"])
    assert result.exit_code == EXIT_USAGE, result.output
    assert "message" in result.output.lower()
