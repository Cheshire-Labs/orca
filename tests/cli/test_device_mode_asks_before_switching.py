"""`orca device mode` shows the server's warning and switches only once the operator agrees."""

from collections.abc import Iterator

import httpx
import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.client import OrcaClient
from orca.operations.device_models import SetDeviceModeResponse
from orca.modes.device_modes import DeviceMode

runner = CliRunner()
_WARNING = "shk1's current state carries over. Switching to live initializes the real shk1."


class _FakeClient:
    def __init__(self) -> None:
        self.confirms: list[bool] = []

    def device_set_mode(self, device_name: str, mode: DeviceMode, *, confirm: bool = False) -> SetDeviceModeResponse:
        self.confirms.append(confirm)
        return SetDeviceModeResponse(device_name=device_name, mode=mode, switched=confirm, warning=_WARNING)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.device.get_client", lambda: stub)
    yield stub


def test_declining_the_prompt_switches_nothing(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["device", "mode", "shk1", "LIVE"], input="n\n")

    assert "carries over" in result.output
    assert fake_client.confirms == [False]


def test_agreeing_switches_with_confirm(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["device", "mode", "shk1", "LIVE"], input="y\n")

    assert result.exit_code == 0, result.output
    assert fake_client.confirms == [False, True]


def test_confirm_flag_switches_without_a_prompt_and_still_prints_the_warning(fake_client: _FakeClient) -> None:
    result = runner.invoke(app, ["device", "mode", "shk1", "LIVE", "--confirm"])

    assert result.exit_code == 0, result.output
    assert "carries over" in result.output
    assert fake_client.confirms == [False, True]


def test_the_switch_waits_as_long_as_the_device_bring_up_takes() -> None:
    read_limits: list[float | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        read_limits.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"device_name": "shk1", "mode": "LIVE", "switched": True, "warning": None})

    class _Client(OrcaClient):
        def _make_http_client(self) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(answer), base_url="http://daemon.test")

    _Client("http://daemon.test").device_set_mode("shk1", DeviceMode.LIVE, confirm=True)

    assert read_limits == [None]
