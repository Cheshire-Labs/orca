"""Tests for POST /mount-topology + POST /workflows on an empty daemon.

The daemon's ingress is split: mounting a topology builds an empty
SystemRuntime; workflows register separately against the mounted topology. This
mirrors the cloud surface (a hosted deployment's POST /api/topology + POST
/api/operations/load-workflow-file).

What these catch:
- Mount actually transitions app.state: health goes from system_loaded
  False to True with a RUNNING runtime, and no workflows are registered.
- Workflow load registers a template on the mounted runtime WITHOUT
  starting any execution; it then becomes runnable via POST /executions.
- A second mount while mounted must 409 (no silent swap).
- Workflow load before any mount must 409 (nothing to register against).
- Error mapping: bad spec -> 400, unknown module -> 404, wrong return
  shape -> 400, a topology that fails to build or start -> 400 carrying
  the reason.
- Mount then unload returns to pre-mount state, and re-mount succeeds.
"""

import asyncio

import pytest
from httpx import AsyncClient

from orca.daemon import routes
from orca.daemon.schemas import (
    ErrorResponse,
    HealthResponse,
    MountTopologyResponse,
    RegisterWorkflowResponse,
    UnloadResponse,
)
from orca.runtime.system_runtime import RuntimeState, SystemRuntime


_TOPOLOGY_SPEC = "tests.daemon.daemon_test_fixture_topology:build_topology"
_WORKFLOW_SPEC = "tests.daemon.daemon_test_fixture_topology:build_workflow"
_BROKEN = "tests.daemon.daemon_test_broken_topologies"
_UNKNOWN_TEACHPOINT_SPEC = f"{_BROKEN}:build_topology_with_an_unknown_teachpoint"
_NO_KIND_SPEC = f"{_BROKEN}:build_topology_with_a_device_that_has_no_kind"


async def test_mount_topology_transitions_health(
    empty_client: AsyncClient,
) -> None:
    """Mount takes the daemon from empty to a RUNNING runtime with no workflows."""
    pre = HealthResponse.model_validate((await empty_client.get("/health")).json())
    assert pre.system_loaded is False
    assert pre.runtime_state is None

    resp = await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    assert resp.status_code == 200
    body = MountTopologyResponse.model_validate(resp.json())
    assert body.spec == _TOPOLOGY_SPEC
    assert body.sim is True
    assert body.runtime_state == RuntimeState.RUNNING

    post = HealthResponse.model_validate((await empty_client.get("/health")).json())
    assert post.system_loaded is True
    assert post.runtime_state == RuntimeState.RUNNING
    assert post.spec == _TOPOLOGY_SPEC

    # No workflow registered yet by mount alone.
    catalog = await empty_client.get("/catalog/workflows")
    assert catalog.status_code == 200
    assert catalog.json() == []

    await empty_client.post("/unload")


async def test_workflow_load_registers_without_running(
    empty_client: AsyncClient,
) -> None:
    """Loading a workflow registers it on the mounted runtime; nothing runs."""
    await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )

    resp = await empty_client.post("/workflows", json={"spec": _WORKFLOW_SPEC})
    assert resp.status_code == 200
    body = RegisterWorkflowResponse.model_validate(resp.json())
    assert body.workflow_name == "simple_workflow"

    catalog = await empty_client.get("/catalog/workflows")
    names = [w["name"] for w in catalog.json()]
    assert "simple_workflow" in names

    # No execution was created by registration.
    execs = await empty_client.get("/operations/list-executions")
    assert execs.status_code == 200
    assert execs.json().get("executions", []) == []

    await empty_client.post("/unload")


async def test_workflow_load_then_run(
    empty_client: AsyncClient,
) -> None:
    """The full mount -> load -> run flow produces an execution."""
    await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    await empty_client.post("/workflows", json={"spec": _WORKFLOW_SPEC})

    run = await empty_client.post(
        "/executions",
        json={"workflow_name": "simple_workflow", "run_mode": "PURE_SIM"},
    )
    assert run.status_code == 200, run.text

    await empty_client.post("/unload")


async def test_mount_twice_rejects_with_409(
    empty_client: AsyncClient,
) -> None:
    """A second mount while mounted must 409; silent swap would be a trap."""
    await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    resp = await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    assert resp.status_code == 409
    err = ErrorResponse.model_validate(resp.json())
    assert "unload first" in err.detail.lower()

    await empty_client.post("/unload")


async def test_workflow_load_before_mount_returns_409(
    empty_client: AsyncClient,
) -> None:
    """Loading a workflow before any topology is mounted must 409."""
    resp = await empty_client.post("/workflows", json={"spec": _WORKFLOW_SPEC})
    assert resp.status_code == 409
    err = ErrorResponse.model_validate(resp.json())
    assert "no system loaded" in err.detail.lower()


async def test_workflow_load_without_mounted_topology_returns_409(
    client: AsyncClient,
) -> None:
    """The injected-runtime path has a runtime but no mounted Topology.

    `client` injects a pre-started runtime directly (no mount-topology), so
    `app.state.topology` is None. Workflow load must 409 rather than crash in
    build_workflow(None).
    """
    resp = await client.post("/workflows", json={"spec": _WORKFLOW_SPEC})
    assert resp.status_code == 409
    err = ErrorResponse.model_validate(resp.json())
    assert "topology" in err.detail.lower()


async def test_mount_bad_spec_format_returns_400(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.post(
        "/mount-topology", json={"spec": "no-colon", "sim": False},
    )
    assert resp.status_code == 400


async def test_mount_unknown_module_returns_404(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.post(
        "/mount-topology",
        json={"spec": "orca.definitely_not_real:build_topology", "sim": False},
    )
    assert resp.status_code == 404


async def test_mount_wrong_return_shape_returns_400(
    empty_client: AsyncClient, tmp_path, monkeypatch,
) -> None:
    """A topology factory that returns a non-Topology must fail with 400."""
    mod = tmp_path / "bad_topology.py"
    mod.write_text("def build_topology(stores):\n    return 42\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    resp = await empty_client.post(
        "/mount-topology", json={"spec": "bad_topology:build_topology", "sim": False},
    )
    assert resp.status_code == 400


async def test_a_topology_that_fails_to_build_returns_400_with_the_reason(
    empty_client: AsyncClient,
) -> None:
    """The builder names the bad teachpoint, and that has to reach the operator."""
    resp = await empty_client.post(
        "/mount-topology", json={"spec": _UNKNOWN_TEACHPOINT_SPEC, "sim": True},
    )
    assert resp.status_code == 400
    err = ErrorResponse.model_validate(resp.json())
    assert "failed to build" in err.detail
    assert "'nowhere'" in err.detail


async def test_a_topology_that_fails_to_start_returns_400_and_is_shut_down(
    empty_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-started runtime would keep listening for device connects."""
    shut_down: list[SystemRuntime] = []
    original_shutdown = SystemRuntime.shutdown

    async def recording_shutdown(self: SystemRuntime, *, confirm: bool = False) -> None:
        shut_down.append(self)
        await original_shutdown(self, confirm=confirm)

    monkeypatch.setattr(SystemRuntime, "shutdown", recording_shutdown)

    resp = await empty_client.post(
        "/mount-topology", json={"spec": _NO_KIND_SPEC, "sim": True},
    )

    assert resp.status_code == 400
    err = ErrorResponse.model_validate(resp.json())
    assert "failed to start" in err.detail
    assert "KIND" in err.detail
    assert len(shut_down) == 1


async def test_a_failed_mount_leaves_the_daemon_ready_for_the_next_one(
    empty_client: AsyncClient,
) -> None:
    for broken in (_UNKNOWN_TEACHPOINT_SPEC, _NO_KIND_SPEC):
        resp = await empty_client.post(
            "/mount-topology", json={"spec": broken, "sim": True},
        )
        assert resp.status_code == 400, broken

    health = HealthResponse.model_validate((await empty_client.get("/health")).json())
    assert health.system_loaded is False

    resp = await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    assert resp.status_code == 200

    await empty_client.post("/unload")


async def test_workflow_load_wrong_name_propagates(
    empty_client: AsyncClient, tmp_path, monkeypatch,
) -> None:
    """A workflow factory returning a non-WorkflowTemplate must fail with 400."""
    await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    mod = tmp_path / "bad_workflow.py"
    mod.write_text("def build_workflow(topology):\n    return 42\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    resp = await empty_client.post(
        "/workflows", json={"spec": "bad_workflow:build_workflow"},
    )
    assert resp.status_code == 400

    await empty_client.post("/unload")


async def test_mount_can_be_repeated_after_unload(
    empty_client: AsyncClient,
) -> None:
    """The mount/unload/mount cycle is a supported operator flow."""
    first = await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    assert first.status_code == 200
    unload = await empty_client.post("/unload")
    assert UnloadResponse.model_validate(unload.json()).status.value == "unloaded"

    second = await empty_client.post(
        "/mount-topology", json={"spec": _TOPOLOGY_SPEC, "sim": True},
    )
    assert second.status_code == 200

    await empty_client.post("/unload")


async def test_a_mount_still_in_progress_refuses_a_second_mount_and_an_unload(
    empty_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI whose mount timed out is told to check status before retrying.

    Status has to show the mount, and the retry has to be refused: two
    overlapping mounts each start a runtime, and only one is kept.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    real_build = routes.build_system

    async def slow_build(**kwargs):
        started.set()
        await release.wait()
        return await real_build(**kwargs)

    monkeypatch.setattr(routes, "build_system", slow_build)
    mount = {"spec": _TOPOLOGY_SPEC, "sim": True}
    first = asyncio.create_task(empty_client.post("/mount-topology", json=mount))
    await asyncio.wait_for(started.wait(), timeout=10.0)

    health = HealthResponse.model_validate((await empty_client.get("/health")).json())
    assert health.mounting == _TOPOLOGY_SPEC
    assert health.system_loaded is False
    for path, body in (("/mount-topology", mount), ("/unload", None)):
        refused = await empty_client.post(path, json=body)
        assert refused.status_code == 409, path
        assert "still in progress" in ErrorResponse.model_validate(refused.json()).detail

    release.set()
    assert (await first).status_code == 200
    health = HealthResponse.model_validate((await empty_client.get("/health")).json())
    assert health.mounting is None
    assert health.system_loaded is True
    await empty_client.post("/unload")


async def test_a_failed_mount_is_no_longer_reported_in_progress(
    empty_client: AsyncClient,
) -> None:
    resp = await empty_client.post(
        "/mount-topology", json={"spec": _NO_KIND_SPEC, "sim": True},
    )
    assert resp.status_code == 400

    health = HealthResponse.model_validate((await empty_client.get("/health")).json())
    assert health.mounting is None
