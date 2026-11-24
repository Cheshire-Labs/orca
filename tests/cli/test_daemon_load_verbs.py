"""CLI dispatch tests for `orca topology mount` and `orca workflow load`.

The route is covered by tests/daemon/test_routes_mount.py. These tests cover
the CLI layer the operator types: backend selection (the local daemon builds
its own folder; cloud takes a source file) and the cloud usage guards.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.output import EXIT_USAGE
from orca.runtime.system_runtime import RuntimeState


runner = CliRunner()


# -- topology mount ----------------------------------------------------------


def test_topology_mount_local_mounts_the_package_folder() -> None:
    with patch("orca.cli.topology.active_backend", return_value="local"), patch(
        "orca.cli.topology.daemon_client"
    ) as mock_client:
        mock_client.return_value.mount_package.return_value = RuntimeState.RUNNING
        result = runner.invoke(app, ["topology", "mount"])
    assert result.exit_code == 0, result.output
    mock_client.return_value.mount_package.assert_called_once_with()
    assert "mounted the package folder" in result.output


def test_topology_mount_local_refuses_an_argument() -> None:
    """The daemon builds its own folder, so a named file would be ignored."""
    with patch("orca.cli.topology.active_backend", return_value="local"), patch(
        "orca.cli.topology.daemon_client"
    ) as mock_client:
        result = runner.invoke(app, ["topology", "mount", "topology.py"])
    assert result.exit_code == EXIT_USAGE, result.output
    assert "no argument" in result.output
    mock_client.return_value.mount_package.assert_not_called()


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


def test_workflow_load_local_points_at_the_package_folder() -> None:
    """The local daemon builds workflows/ on mount; it has no route to load one."""
    with patch("orca.cli.backend.active_backend", return_value="local"):
        result = runner.invoke(app, ["workflow", "load", "wf.py"])
    assert result.exit_code == EXIT_USAGE, result.output
    assert "workflows/" in result.output


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
