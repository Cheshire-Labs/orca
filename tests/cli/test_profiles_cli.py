"""`orca profiles ...` and `orca run --profile <name>`, one client for both servers."""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from orca.cli import output
from orca.cli.app import app
from orca.cli.client import OrcaClient
from orca.operations.execution_models import ExecutionRecordModel
from orca.variables.deployment_profile import DeploymentProfile

runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.added: DeploymentProfile | None = None
        self.updated: DeploymentProfile | None = None
        self.deleted: str | None = None

    def profiles_list(self) -> list[DeploymentProfile]:
        return [DeploymentProfile(name="qc", variables={"speed": 5})]

    def profiles_get(self, name: str) -> DeploymentProfile:
        return DeploymentProfile(name=name, description="d", variables={"speed": 5})

    def profiles_add(
        self, profile: DeploymentProfile, *, reason: str | None = None,
    ) -> DeploymentProfile:
        self.added = profile
        return profile

    def profiles_update(
        self, profile: DeploymentProfile, *, reason: str | None = None,
    ) -> DeploymentProfile:
        self.updated = profile
        return profile

    def profiles_delete(self, name: str, *, reason: str | None = None) -> None:
        self.deleted = name


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.profile.get_client", lambda: stub)
    output.set_mode(output.OutputMode.TABLE)
    yield stub


def test_list_renders_a_table(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["profiles", "list"])

    assert result.exit_code == 0, result.output
    assert "qc" in result.stdout


def test_show_prints_the_values(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["profiles", "show", "qc"])

    assert result.exit_code == 0, result.output
    assert "speed" in result.stdout


def test_create_sends_typed_values_and_computed_expressions(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, [
        "profiles", "create", "qc", "--description", "fast",
        "--vars", "speed=500,dry=true,label=a",
        "--computed", "twice=speed * 2",
        "--computed", "limit=min(speed, 9)",
    ])

    assert result.exit_code == 0, result.output
    assert fake_client.added == DeploymentProfile(
        name="qc", description="fast",
        variables={"speed": 500, "dry": True, "label": "a"},
        computed={"twice": "speed * 2", "limit": "min(speed, 9)"},
    )


def test_a_computed_entry_with_no_equals_sign_is_refused(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["profiles", "create", "qc", "--computed", "nope"])

    assert result.exit_code == output.EXIT_USAGE
    assert fake_client.added is None


def test_update_sends_the_whole_new_body(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["profiles", "update", "qc", "--vars", "speed=9"])

    assert result.exit_code == 0, result.output
    assert fake_client.updated == DeploymentProfile(name="qc", variables={"speed": 9})


def test_delete_names_the_profile(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["profiles", "delete", "qc"])

    assert result.exit_code == 0, result.output
    assert fake_client.deleted == "qc"


def test_run_sends_the_profile_name() -> None:
    client = MagicMock()
    client.submit_workflow.return_value = ExecutionRecordModel(
        id="e-1", workflow_name="wf", status="running", error=None,
    )

    with patch("orca.cli.backend.get_client", return_value=client):
        result = runner.invoke(app, ["run", "wf", "--run-mode", "CHECK_RUN", "--profile", "qc"])

    assert result.exit_code == 0, result.output
    assert client.submit_workflow.call_args.kwargs["deployment_profile"] == "qc"


def test_the_client_puts_the_name_on_the_submit_body() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={
            "id": "s", "execution_id": "e", "workflow_name": "wf", "group_count": 0,
            "status": "ACCEPTED", "batch_mode": "STANDALONE", "submitted_at": "t",
            "operator_id": None, "deployment_profile": "qc", "run_mode": "CHECK_RUN",
            "unsettled": [],
        })

    client = OrcaClient("http://server")
    real_client = httpx.Client
    with patch(
        "orca.cli.client.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    ):
        client.submit_workflow("wf", run_mode="CHECK_RUN", deployment_profile="qc")

    assert b'"deployment_profile":"qc"' in bodies[0].replace(b" ", b"")
