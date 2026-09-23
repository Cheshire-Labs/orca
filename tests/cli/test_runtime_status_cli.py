"""CLI tests for ``orca runtime status`` (cloud-only).

Validates dispatch + rendering. Wire-shape parity with a hosted deployment's
``GET /api/runtime/status`` is pinned by the cloud backend's typed
mirror.
"""

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.control_plane import RuntimeStatusResponseDTO


runner = CliRunner()


class _FakeCloudClient:
    """Records calls + returns a canned RuntimeStatusResponseDTO."""

    def __init__(self, payload: RuntimeStatusResponseDTO) -> None:
        self.runtime_status_called: int = 0
        self._payload = payload

    def runtime_status(self) -> RuntimeStatusResponseDTO:
        self.runtime_status_called += 1
        return self._payload


@pytest.fixture
def fake_built_true(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeCloudClient]:
    stub = _FakeCloudClient(
        RuntimeStatusResponseDTO(built=True, last_build_error=None),
    )
    monkeypatch.setattr("orca.cli.runtime.get_client", lambda: stub)
    yield stub


@pytest.fixture
def fake_built_false(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeCloudClient]:
    stub = _FakeCloudClient(
        RuntimeStatusResponseDTO(
            built=False,
            last_build_error={
                "type": "ModuleNotFoundError",
                "message": "No module named 'deployment_package.system'",
                "hint": (
                    "Deployment package missing. Drop system.py on disk + "
                    "submit topology + workflows, then runtime_reload."
                ),
            },
        ),
    )
    monkeypatch.setattr("orca.cli.runtime.get_client", lambda: stub)
    yield stub


def test_runtime_status_built_table_mode(fake_built_true: _FakeCloudClient) -> None:
    result = runner.invoke(app, ["runtime", "status"])
    assert result.exit_code == 0, result.output
    assert fake_built_true.runtime_status_called == 1
    assert "runtime built" in result.output


def test_runtime_status_built_json_mode(fake_built_true: _FakeCloudClient) -> None:
    result = runner.invoke(app, ["--json", "runtime", "status"])
    assert result.exit_code == 0, result.output
    assert '"built": true' in result.output


def test_runtime_status_not_built_table_mode(fake_built_false: _FakeCloudClient) -> None:
    result = runner.invoke(app, ["runtime", "status"])
    assert result.exit_code == 0, result.output
    assert "runtime NOT built" in result.output
    assert "ModuleNotFoundError" in result.output
    assert "deployment_package" in result.output
    assert "Deployment package missing" in result.output


def test_runtime_status_not_built_json_mode(fake_built_false: _FakeCloudClient) -> None:
    result = runner.invoke(app, ["--json", "runtime", "status"])
    assert result.exit_code == 0, result.output
    assert '"built": false' in result.output
    assert '"ModuleNotFoundError"' in result.output


def test_runtime_status_help() -> None:
    result = runner.invoke(app, ["runtime", "status", "--help"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "runtime" in out
    assert "status" in out
