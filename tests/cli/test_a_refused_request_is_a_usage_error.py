"""A request this CLI's models refuse before sending says which field, like the server's 422 does. A refused answer says the versions differ."""

from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import BaseModel
from typer.testing import CliRunner

from orca.cli import output
from orca.cli.app import app
from orca.cli.client import OrcaClient

runner = CliRunner()


class _Plain(OrcaClient):
    def __init__(self, transport: httpx.MockTransport) -> None:
        super().__init__("http://daemon")
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, base_url=self._base_url)


class _Named(BaseModel):
    name: str


class _BuildsABadRequest:
    def runtime_status(self) -> None:
        _Named.model_validate({"name": 3})


def test_a_request_the_models_refuse_names_the_field_and_exits_as_a_server_422_would() -> None:
    with patch("orca.cli.runtime.get_client", return_value=_BuildsABadRequest()):
        result = runner.invoke(app, ["runtime", "status"])

    assert result.exit_code == output.exit_code_for_status(422)
    assert "validation_error: name:" in result.output
    assert "does not match this version of orca" not in result.output


def test_an_answer_that_fails_to_parse_still_says_the_versions_differ() -> None:
    def newer_server(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"built": True, "added_in_a_later_release": 1})

    with patch("orca.cli.runtime.get_client", return_value=_Plain(httpx.MockTransport(newer_server))):
        result = runner.invoke(app, ["runtime", "status"])

    assert result.exit_code == output.EXIT_GENERIC
    assert "does not match this version of orca" in result.output
    assert "Traceback" not in result.output


def test_a_refused_request_exits_as_a_usage_error_whether_the_server_says_400_or_422() -> None:
    assert output.exit_code_for_status(422) == output.exit_code_for_status(400) == output.EXIT_USAGE
