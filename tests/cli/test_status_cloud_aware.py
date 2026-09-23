"""`orca status` is backend-aware.

Under a cloud backend, `orca status` renders the hosted runtime-lifecycle
snapshot instead of emitting the local "no daemon running" advice.
"""

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.control_plane import RuntimeStatusResponseDTO


runner = CliRunner()


class _FakeCloud:
    def __init__(self, payload: RuntimeStatusResponseDTO) -> None:
        self.calls = 0
        self._payload = payload

    def runtime_status(self) -> RuntimeStatusResponseDTO:
        self.calls += 1
        return self._payload


@pytest.fixture
def cloud_built(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeCloud]:
    stub = _FakeCloud(RuntimeStatusResponseDTO(built=True, last_build_error=None))
    monkeypatch.setattr("orca.cli.status.resolve_backend", lambda _b: "cloud")
    monkeypatch.setattr("orca.cli.status.cloud_client", lambda: stub)
    yield stub


def test_status_cloud_renders_runtime_status_not_daemon_advice(
    cloud_built: _FakeCloud,
) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert cloud_built.calls == 1
    assert "no daemon running" not in result.output
    assert "cloud runtime" in result.output


def test_status_cloud_json_mode(cloud_built: _FakeCloud) -> None:
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    assert '"built": true' in result.output


def test_status_cloud_not_built_shows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _FakeCloud(
        RuntimeStatusResponseDTO(
            built=False,
            last_build_error={
                "type": "ModuleNotFoundError",
                "message": "No module named 'deployment_package.system'",
                "hint": "Drop system.py + submit topology, then runtime_reload.",
            },
        ),
    )
    monkeypatch.setattr("orca.cli.status.resolve_backend", lambda _b: "cloud")
    monkeypatch.setattr("orca.cli.status.cloud_client", lambda: stub)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "ModuleNotFoundError" in result.output
    assert "no daemon running" not in result.output
