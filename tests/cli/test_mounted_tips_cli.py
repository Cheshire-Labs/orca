"""CLI dispatch for ``orca device mounted-tips``.

The terminal is where an operator sees whether a channel number was observed or
counted from zero; the flag exists to be read by a person, so the rendered line
is the surface, not an implementation detail.
"""

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from orca.cli.app import app
from orca.operations.device_models import GetMountedTipsResponse, MountedTipReadDTO


runner = CliRunner()


def _response(*inferred: bool) -> GetMountedTipsResponse:
    return GetMountedTipsResponse(
        device_name="mlstar_1", provenance="known",
        mounted=[
            MountedTipReadDTO(
                channel=channel, tip_rack="tips_96", position="A1",
                channel_is_inferred=flag,
            )
            for channel, flag in enumerate(inferred)
        ],
    )


class _FakeClient:
    def __init__(self) -> None:
        self.response = _response(False)

    def device_get_mounted_tips(self, device_name: str) -> GetMountedTipsResponse:
        return self.response


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeClient]:
    stub = _FakeClient()
    monkeypatch.setattr("orca.cli.device.get_client", lambda: stub)
    yield stub


def test_an_observed_channel_is_not_flagged(fake_client) -> None:
    result = runner.invoke(app, ["device", "mounted-tips", "mlstar_1"])

    assert result.exit_code == 0, result.output
    assert "channel 0: tips_96 A1" in result.stderr
    assert "inferred" not in result.stderr


def test_a_guessed_channel_says_so_on_the_line_it_belongs_to(fake_client) -> None:
    """Per channel, not per head: a mixed head must not flag the observed one."""
    fake_client.response = _response(True, False)

    result = runner.invoke(app, ["device", "mounted-tips", "mlstar_1"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stderr.splitlines() if "tips_96" in line]
    assert len(lines) == 2, result.stderr
    assert "inferred" in lines[0]
    assert "inferred" not in lines[1]


def test_an_empty_head_says_so(fake_client) -> None:
    fake_client.response = _response()

    result = runner.invoke(app, ["device", "mounted-tips", "mlstar_1"])

    assert result.exit_code == 0, result.output
    assert "no tips recorded" in result.stderr
