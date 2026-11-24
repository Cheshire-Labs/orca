"""`orca events list` reads GET /api/events, the stored events either server keeps."""

import json
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.client import OrcaClient
from orca.operations.events import PastEvent

runner = CliRunner()

_EVENT = PastEvent(
    cursor=7, event_name="EXECUTION.STARTED", execution_id="exec-1", timestamp=1.0,
    entity_type="execution", entity_id="exec-1", status="STARTED", context={},
)


class _Wired(OrcaClient):
    def __init__(self, base_url: str, transport: httpx.MockTransport) -> None:
        super().__init__(base_url)
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, base_url=self._base_url)


def test_the_client_sends_the_filters_as_query_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_EVENT.model_dump(mode="json")])

    client = _Wired("http://orca.test", httpx.MockTransport(handler))
    events = client.events_list(since=3, execution_id="exec-1", limit=10)

    assert events == [_EVENT]
    assert seen[0].url.path == "/api/events"
    assert dict(seen[0].url.params) == {"since": "3", "execution_id": "exec-1", "limit": "10", "tail": "false"}


def test_the_verb_passes_its_options_and_prints_each_event_as_json() -> None:
    with patch("orca.cli.events.get_client") as get_client:
        get_client.return_value.events_list.return_value = [_EVENT]
        result = runner.invoke(app, ["--json", "events", "list", "--execution", "exec-1", "--tail", "--limit", "5"])

    assert result.exit_code == 0, result.output
    get_client.return_value.events_list.assert_called_once_with(since=0, execution_id="exec-1", limit=5, tail=True)
    assert json.loads(result.output) == [_EVENT.model_dump(mode="json")]
