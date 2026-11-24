"""OrcaClient unit tests for the reads and labware clears.

Each test wires a MockTransport that asserts the (method, path) the client
hits and returns a canned daemon-shape body, then asserts the parsed
return DTO. Mirrors the recording style of `tests/cli/test_the_client_routes.py`
but checks the parse path end to end (not just the wire address).
"""

from collections.abc import Callable

import httpx

from orca.cli.client import OrcaClient
from orca.operations.labware_models import (
    GetLabwareJourneyResponse,
)
from orca.operations.wire_models import (
    MethodTemplateDTO,
    RuntimeStatusResponse,
    TopologySourceResponse,
    TopologyViewResponse,
    WorkflowTemplateDTO,
)
from orca.operations.ops_history import (
    SearchOpsHistoryResponse,
)


class _FakeLocal(OrcaClient):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__("http://test-daemon", timeout=30.0)
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
        "GET", "/api/methods",
        {"methods": [
            {"workflow_name": "smc", "name": "incubate", "failure_policy": "PAUSE"},
        ]},
    )
    out = list(client.methods_list())
    assert len(out) == 1
    assert isinstance(out[0], MethodTemplateDTO)
    assert out[0].failure_policy == "PAUSE"


def test_method_get_parses_summary() -> None:
    client = _client(
        "GET", "/api/methods/incubate",
        {"method": {"workflow_name": "smc", "name": "incubate", "failure_policy": "ABORT"}},
    )
    out = client.method_get("incubate")
    assert isinstance(out, MethodTemplateDTO)
    assert out.name == "incubate"


def test_workflow_get_parses_summary() -> None:
    client = _client(
        "GET", "/api/workflows/smc",
        {"workflow": {"name": "smc", "entry_thread_template_names": ["t1"]}},
    )
    out = client.workflow_get("smc")
    assert isinstance(out, WorkflowTemplateDTO)
    assert out.entry_thread_template_names == ("t1",)


def test_topology_get_view() -> None:
    client = _client(
        "GET", "/api/topology",
        {"devices": [{"name": "shk1", "declared_kind": "Shaker", "declared_interfaces": ["IShaker"],
                      "mounting_locations": [], "declared_interfaces_are_class_defaults": False}],
         "transporters": [], "resource_pools": [], "locations": [],
         "labware_templates": []},
    )
    out = client.topology_get()
    assert isinstance(out, TopologyViewResponse)
    assert out.devices[0].name == "shk1"


def test_topology_get_source() -> None:
    client = _client(
        "GET", "/api/topology",
        {"source": "pkg:build_topology", "last_modified_sha": None,
         "last_modified_at": None},
    )
    out = client.topology_get(source=True)
    assert isinstance(out, TopologySourceResponse)
    assert out.source == "pkg:build_topology"


def test_runtime_status_parses() -> None:
    client = _client(
        "GET", "/api/runtime/status", {"built": True, "last_build_error": None},
    )
    out = client.runtime_status()
    assert isinstance(out, RuntimeStatusResponse)
    assert out.built is True


def test_ops_history_search_omits_none_filters() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/ops-history"
        seen.update(request.url.params)
        return httpx.Response(200, json={"records": []})

    client = _FakeLocal(handler)
    out = client.ops_history_search(execution_id="exec-1", device_name="shk1")
    assert isinstance(out, SearchOpsHistoryResponse)
    # None filters are omitted from the query; only the two set ones ride.
    assert seen == {"execution_id": "exec-1", "device_name": "shk1"}


def test_labware_journey_parses() -> None:
    client = _client(
        "GET", "/api/labware-instances/lw-1/journey",
        {"labware_id": "lw-1", "entries": []},
    )
    out = client.labware_journey("lw-1")
    assert isinstance(out, GetLabwareJourneyResponse)
    assert out.labware_id == "lw-1"


def test_labware_journey_sends_kinds() -> None:
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/labware-instances/lw-1/journey"
        for key in request.url.params:
            seen[key] = request.url.params.get_list(key)
        return httpx.Response(200, json={"labware_id": "lw-1", "entries": []})

    client = _FakeLocal(handler)
    client.labware_journey("lw-1", kinds=["move"])
    assert seen == {"kinds": ["move"], "include_driver_snapshots": ["false"]}


def test_labware_clear_all_parses() -> None:
    client = _client(
        "POST", "/api/labware-instances/clear-all", {"cleared": ["lw-1"]},
    )
    out = client.labware_clear_all(force=True)
    assert out == {"cleared_labware_ids": ["lw-1"]}


def test_labware_discharge_parses() -> None:
    client = _client(
        "POST", "/api/labware-instances/lw-1/discharge",
        {
            "labware_id": "lw-1", "status": "discharged",
            "blockers": [], "blocker_count": 0, "blockers_fingerprint": "none",
        },
    )
    out = client.labware_discharge("lw-1")
    assert out == {"cleared_labware_ids": ["lw-1"]}


def test_labware_clear_submission_parses_both_lists() -> None:
    client = _client(
        "POST", "/api/submissions/sub-1/clear-labware",
        {"cleared": ["lw-1"], "preserved_reuse_bound": ["lw-2"]},
    )
    out = client.labware_clear_submission("sub-1")
    assert out == {"cleared": ["lw-1"], "preserved_reuse_bound": ["lw-2"]}
