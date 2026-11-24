"""`orca method execute` turns `--hook`, `--no-hooks` and `--profile` into the client call."""
from collections.abc import Iterator, Mapping, Sequence

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.operations.submission_models import MethodExecutionResponse
from orca.operations.wire_models import RunModeStr

runner = CliRunner()
BASE = ["method", "execute", "reading", "read_method", "--labware-start", '{"plate": "pad_1"}',
        "--labware-end", '{"plate": "pad_1"}', "--run-mode", "SIM"]


class _Recorder:
    def __init__(self) -> None:
        self.event_hooks_sent: list[Sequence[str] | None] = []
        self.deployment_profile: str | None = None

    def submit_method(
        self, workflow_name: str, method_name: str, labware_start: Mapping[str, str], labware_end: Mapping[str, str],
        variables: Mapping[str, str] | None = None, *, run_mode: RunModeStr,
        deployment_profile: str | None = None, event_hooks: Sequence[str] | None = None,
    ) -> MethodExecutionResponse:
        self.event_hooks_sent.append(event_hooks)
        self.deployment_profile = deployment_profile
        return MethodExecutionResponse(execution_id="exec-1", workflow_name="w", status="running")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    recorder = _Recorder()
    monkeypatch.setattr("orca.cli.backend.get_client", lambda: recorder)
    yield recorder


@pytest.mark.parametrize(("flags", "sent"), [
    ([], None),
    (["--no-hooks"], []),
    (["--hook", "ACTION.COMPLETED"], ["ACTION.COMPLETED"]),
    (["--hook", "ACTION.COMPLETED", "--hook", "CUSTOM.read_done.EMITTED"], ["ACTION.COMPLETED", "CUSTOM.read_done.EMITTED"]),
])
def test_the_hook_flags_choose_the_handlers_sent(client: _Recorder, flags: list[str], sent: list[str] | None) -> None:
    result = runner.invoke(app, [*BASE, *flags])
    assert result.exit_code == 0, result.output
    assert client.event_hooks_sent == [sent]


def test_hook_and_no_hooks_together_is_a_usage_error(client: _Recorder) -> None:
    result = runner.invoke(app, [*BASE, "--hook", "ACTION.COMPLETED", "--no-hooks"])
    assert result.exit_code != 0
    assert client.event_hooks_sent == []


def test_the_profile_is_sent(client: _Recorder) -> None:
    assert runner.invoke(app, [*BASE, "--profile", "high_throughput"]).exit_code == 0
    assert client.deployment_profile == "high_throughput"
