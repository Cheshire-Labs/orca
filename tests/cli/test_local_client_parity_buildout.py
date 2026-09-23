"""LocalDaemonClient unit tests for the backend-parity build-out methods.

Each test wires a MockTransport that asserts the (method, path) the client
hits and returns a canned daemon-shape body, then asserts the parsed
return DTO. Mirrors the recording style of `tests/cli/test_command_parity.py`
but checks the parse path end to end (not just the wire address).
"""

from collections.abc import Callable

import httpx

from orca.cli.client import LocalDaemonClient
from orca.cli.control_plane import (
    LabwareJourneyResponseDTO,
    MethodSummaryDTO,
    OpsHistoryGetResponseDTO,
    OpsHistorySearchResponseDTO,
    RuntimeStatusResponseDTO,
    TopologySourceDTO,
    TopologyViewDTO,
    WorkflowSummaryDTO,
)


class _FakeLocal(LocalDaemonClient):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        from orca.daemon.lifecycle import DaemonInfo
        self._info = DaemonInfo(pid=0, port=0, started_at=0.0)
        self._base_url = "http://test-daemon"
        self._timeout = 30.0
        self._handler = handler

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self._handler),
            base_url=self._base_url, timeout=self._timeout,
        )


def _client(
    expect_method: str, expect_path: str, body: object,
) -> _FakeLocal:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == expect_method, request.method
        assert request.url.path == expect_path, request.url.path
        return httpx.Response(200, json=body)
    return _FakeLocal(handler)


def test_methods_list_parses_summary() -> None:
    client = _client(
        "GET", "/operations/list-methods",
        {"methods": [
            {"workflow_name": "smc", "name": "incubate", "failure_policy": "PAUSE"},
        ]},
    )
    out = list(client.methods_list())
    assert len(out) == 1
    assert isinstance(out[0], MethodSummaryDTO)
    assert out[0].failure_policy == "PAUSE"


def test_method_get_parses_summary() -> None:
    client = _client(
        "POST", "/operations/get-method",
        {"method": {"workflow_name": "smc", "name": "incubate", "failure_policy": "STOP"}},
    )
    out = client.method_get("incubate")
    assert isinstance(out, MethodSummaryDTO)
    assert out.name == "incubate"


def test_workflow_get_parses_summary() -> None:
    client = _client(
        "POST", "/operations/get-workflow",
        {"workflow": {"name": "smc", "entry_thread_template_names": ["t1"]}},
    )
    out = client.workflow_get("smc")
    assert isinstance(out, WorkflowSummaryDTO)
    assert out.entry_thread_template_names == ["t1"]


def test_topology_get_view() -> None:
    client = _client(
        "GET", "/topology",
        {"devices": [{"name": "shk1", "type_name": "Shaker", "is_initialized": True,
                      "is_busy": False, "effective_mode": "PURE_SIM",
                      "position_ids": [], "loaded_labware_ids": []}],
         "transporters": [], "resource_pools": [], "locations": [],
         "labware_templates": []},
    )
    out = client.topology_get()
    assert isinstance(out, TopologyViewDTO)
    assert out.devices[0].name == "shk1"


def test_topology_get_source() -> None:
    client = _client(
        "GET", "/topology",
        {"source": "pkg:build_topology", "last_modified_sha": None,
         "last_modified_at": None},
    )
    out = client.topology_get(source=True)
    assert isinstance(out, TopologySourceDTO)
    assert out.source == "pkg:build_topology"


def test_runtime_status_parses() -> None:
    client = _client(
        "GET", "/runtime/status", {"built": True, "last_build_error": None},
    )
    out = client.runtime_status()
    assert isinstance(out, RuntimeStatusResponseDTO)
    assert out.built is True


def test_ops_history_get_parses() -> None:
    client = _client(
        "POST", "/operations/list-ops-history",
        {"execution_id": "exec-1", "records": []},
    )
    out = client.ops_history_get("exec-1")
    assert isinstance(out, OpsHistoryGetResponseDTO)
    assert out.execution_id == "exec-1"


def test_ops_history_search_omits_none_filters() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/operations/search-ops-history"
        import json
        seen.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"records": []})

    client = _FakeLocal(handler)
    out = client.ops_history_search(execution_id="exec-1", device_name="shk1")
    assert isinstance(out, OpsHistorySearchResponseDTO)
    # None filters are omitted from the body; only the two set ones ride.
    assert seen == {"execution_id": "exec-1", "device_name": "shk1"}


def test_labware_journey_parses() -> None:
    client = _client(
        "POST", "/operations/get-labware-journey",
        {"labware_id": "lw-1", "entries": []},
    )
    out = client.labware_journey("lw-1")
    assert isinstance(out, LabwareJourneyResponseDTO)
    assert out.labware_id == "lw-1"


def test_labware_journey_sends_kinds() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"labware_id": "lw-1", "entries": []})

    client = _FakeLocal(handler)
    client.labware_journey("lw-1", kinds=["move"])
    assert seen == {"labware_id": "lw-1", "kinds": ["move"]}


def test_labware_clear_all_parses() -> None:
    client = _client(
        "POST", "/labware/runtime/clear-all", {"cleared_labware_ids": ["lw-1"]},
    )
    out = client.labware_clear_all(force=True)
    assert out == {"cleared_labware_ids": ["lw-1"]}


def test_labware_discharge_parses() -> None:
    client = _client(
        "POST", "/labware/runtime/discharge", {"cleared_labware_ids": ["lw-1"]},
    )
    out = client.labware_discharge("lw-1")
    assert out == {"cleared_labware_ids": ["lw-1"]}


def test_labware_clear_submission_parses_both_lists() -> None:
    client = _client(
        "POST", "/labware/runtime/clear-submission",
        {"cleared": ["lw-1"], "preserved_reuse_bound": ["lw-2"]},
    )
    out = client.labware_clear_submission("sub-1")
    assert out == {"cleared": ["lw-1"], "preserved_reuse_bound": ["lw-2"]}
