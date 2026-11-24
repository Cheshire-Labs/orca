"""POST /runtime/mount-package builds the daemon's package folder; POST /runtime/unload tears it down.

The package folder is `ORCA_PACKAGE_DIR` or the folder the daemon started in.
A mount builds its `topology.py` and every
`workflows/<name>.py`, the same way a hosted server builds its worktree.
Picking up edited files is a reload, tested in test_the_daemon_reloads_a_changed_package.py.
"""

import asyncio
import dataclasses
from pathlib import Path

import pytest
from httpx import AsyncClient

from orca.runtime import runtime_holder
from orca.daemon.app import create_app
from orca.daemon.daemon_state import DaemonState
from orca.operations.wire_models import HealthResponse, MountPackageResponse, UnloadResponse
from orca.events.emitter import IEventSink
from orca.runtime.store_factory import default_package_root
from orca.runtime.system_runtime import RuntimeState, SystemRuntime
from tests.daemon.started_daemon import started_daemon
from tests.daemon.fixture_package import BROKEN, SLOW_WORKFLOW, SIMPLE_WORKFLOW, write_package

# The system build must reach every position these runs touch.
pytestmark = pytest.mark.positions_bound_by_the_build

_UNKNOWN_TEACHPOINT = f"{BROKEN}:build_topology_with_an_unknown_teachpoint"
_FAILS_AT_START = f"{BROKEN}:build_topology_with_an_arm_that_fails_at_start"
_MOUNT: dict[str, bool] = {}


async def _health(client: AsyncClient) -> HealthResponse:
    return HealthResponse.model_validate((await client.get("/health")).json())


async def _workflow_names(client: AsyncClient) -> list[str]:
    catalog = await client.get("/api/workflows")
    assert catalog.status_code == 200, catalog.text
    return sorted(w["name"] for w in catalog.json()["workflows"])


async def test_a_mount_builds_the_topology_and_every_workflow_in_the_folder(
    empty_client: AsyncClient,
) -> None:
    root = write_package(workflows={**SIMPLE_WORKFLOW, **SLOW_WORKFLOW})
    pre = await _health(empty_client)
    assert pre.system_loaded is False
    assert pre.package_dir == str(root)

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 200, resp.text
    body = MountPackageResponse.model_validate(resp.json())
    assert body.package_dir == str(root)
    assert body.runtime_state == RuntimeState.RUNNING
    post = await _health(empty_client)
    assert post.system_loaded is True
    assert post.runtime_state == RuntimeState.RUNNING
    assert await _workflow_names(empty_client) == ["simple_workflow", "slow_window_workflow"]
    # Building registers workflows; it starts nothing.
    execs = await empty_client.get("/api/executions")
    assert execs.json().get("executions", []) == []
    await empty_client.post("/api/runtime/unload")


async def test_neither_the_mount_nor_health_reports_a_sim_flag(empty_client: AsyncClient) -> None:
    """Each submission picks its run mode, so a mount-wide sim flag would only mislead."""
    write_package()

    mounted = await empty_client.post("/api/runtime/mount-package", json={})
    health = (await empty_client.get("/health")).json()
    await empty_client.post("/api/runtime/unload")

    assert mounted.status_code == 200, mounted.text
    assert "sim" not in mounted.json()
    assert "sim" not in health


async def test_a_mounted_workflow_runs(empty_client: AsyncClient) -> None:
    write_package()
    await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    run = await empty_client.post(
        "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"},
    )

    assert run.status_code == 200, run.text
    await empty_client.post("/api/runtime/unload")


async def test_reservoirs_a_workflow_file_declares_reach_the_system(
    empty_client: AsyncClient,
) -> None:
    root = write_package()
    workflow_file = root / "workflows" / "simple_workflow.py"
    workflow_file.write_text(
        workflow_file.read_text(encoding="utf-8")
        + "from tests.test_helpers import create_test_plate_template\n"
        + "reservoirs = [create_test_plate_template('trough')]\n",
        encoding="utf-8",
    )
    await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    topology = (await empty_client.get("/api/topology")).json()

    assert "trough" in [lt["name"] for lt in topology["labware_templates"]]
    await empty_client.post("/api/runtime/unload")


async def test_the_topology_source_is_the_folder_topology_file(
    empty_client: AsyncClient,
) -> None:
    root = write_package()
    await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    resp = await empty_client.get("/api/topology", params={"source": True})

    assert resp.json()["source"] == (root / "topology.py").read_text(encoding="utf-8")
    await empty_client.post("/api/runtime/unload")


async def test_the_topology_source_is_read_before_any_mount(empty_client: AsyncClient) -> None:
    root = write_package()

    resp = await empty_client.get("/api/topology", params={"source": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == (root / "topology.py").read_text(encoding="utf-8")


async def test_the_topology_source_is_read_after_a_failed_mount(empty_client: AsyncClient) -> None:
    root = write_package(topology=_UNKNOWN_TEACHPOINT, workflows={})
    assert (await empty_client.post("/api/runtime/mount-package", json=_MOUNT)).status_code == 400

    resp = await empty_client.get("/api/topology", params={"source": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == (root / "topology.py").read_text(encoding="utf-8")


async def test_a_file_added_while_mounted_is_built_after_unload_and_mount(
    empty_client: AsyncClient,
) -> None:
    root = write_package()
    await empty_client.post("/api/runtime/mount-package", json=_MOUNT)
    write_package(root, workflows=SLOW_WORKFLOW)
    assert await _workflow_names(empty_client) == ["simple_workflow"]

    await empty_client.post("/api/runtime/unload")
    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 200, resp.text
    assert await _workflow_names(empty_client) == ["simple_workflow", "slow_window_workflow"]
    await empty_client.post("/api/runtime/unload")


async def test_mount_twice_rejects_with_409(empty_client: AsyncClient) -> None:
    """A second mount while mounted must 409; a silent swap would be a trap."""
    write_package()
    await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 409
    assert "unload first" in resp.json()["detail"]["message"].lower()
    await empty_client.post("/api/runtime/unload")


async def test_a_folder_with_no_topology_returns_404(empty_client: AsyncClient) -> None:
    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 404
    assert "no topology.py" in resp.json()["detail"]["message"]


async def test_a_topology_of_the_wrong_shape_returns_400(empty_client: AsyncClient) -> None:
    root = default_package_root()
    (root / "topology.py").write_text("def build_topology():\n    return 42\n", encoding="utf-8")

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 400
    err = resp.json()["detail"]
    assert err["code"] == "package_load_failed"
    assert "expected orca.sdk.build.Topology" in err["message"]


async def test_a_workflow_of_the_wrong_shape_fails_the_mount(empty_client: AsyncClient) -> None:
    root = write_package(workflows={})
    (root / "workflows").mkdir()
    (root / "workflows" / "bad.py").write_text(
        "def build_workflow(topology):\n    return 42\n", encoding="utf-8",
    )

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 400
    assert "expected WorkflowTemplate" in resp.json()["detail"]["message"]
    assert (await _health(empty_client)).system_loaded is False


async def test_a_topology_that_fails_to_build_returns_400_with_the_reason(
    empty_client: AsyncClient,
) -> None:
    """The builder names the bad teachpoint, and that has to reach the operator."""
    write_package(topology=_UNKNOWN_TEACHPOINT, workflows={})

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 400
    err = resp.json()["detail"]
    assert err["code"] == "package_load_failed"
    assert "failed to build" in err["message"]
    assert "'nowhere'" in err["message"]


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
    write_package(topology=_FAILS_AT_START, workflows={})

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 400
    err = resp.json()["detail"]
    assert "failed to start" in err["message"]
    assert "refuses its move defaults" in err["message"]
    assert len(shut_down) == 1


async def test_a_failed_mount_leaves_the_daemon_ready_for_the_fixed_files(
    empty_client: AsyncClient,
) -> None:
    root = default_package_root()
    for broken in (_UNKNOWN_TEACHPOINT, _FAILS_AT_START):
        write_package(root, topology=broken, workflows={})
        resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)
        assert resp.status_code == 400, broken
    assert (await _health(empty_client)).system_loaded is False

    write_package(root)
    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 200, resp.text
    await empty_client.post("/api/runtime/unload")


async def test_a_folder_named_like_an_imported_module_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca.daemon.app import create_app
    from tests.daemon.started_daemon import started_daemon

    root = write_package(tmp_path / "json")
    async with started_daemon(create_app(package_root=root)) as c:
        resp = await c.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 400
    assert "already an imported module" in resp.json()["detail"]["message"]


async def test_mount_can_be_repeated_after_unload(empty_client: AsyncClient) -> None:
    write_package()
    first = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)
    assert first.status_code == 200
    unload = await empty_client.post("/api/runtime/unload")
    assert UnloadResponse.model_validate(unload.json()).status.value == "unloaded"

    second = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert second.status_code == 200, second.text
    await empty_client.post("/api/runtime/unload")


async def test_a_mount_still_in_progress_refuses_a_second_mount_and_an_unload(
    empty_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI whose mount timed out is told to check status before retrying.

    Status has to show the mount, and the retry has to be refused: two
    overlapping mounts each start a runtime, and only one is kept.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    real_build = runtime_holder.build_system_from_package

    async def slow_build(*args, **kwargs):
        started.set()
        await release.wait()
        return await real_build(*args, **kwargs)

    monkeypatch.setattr(runtime_holder, "build_system_from_package", slow_build)
    write_package()
    first = asyncio.create_task(empty_client.post("/api/runtime/mount-package", json=_MOUNT))
    await asyncio.wait_for(started.wait(), timeout=10.0)

    health = await _health(empty_client)
    assert health.mounting is True
    assert health.system_loaded is False
    for path, body in (("/api/runtime/mount-package", _MOUNT), ("/api/runtime/unload", None)):
        refused = await empty_client.post(path, json=body)
        assert refused.status_code == 409, path
        assert "still in progress" in refused.json()["detail"]["message"]

    release.set()
    assert (await first).status_code == 200
    health = await _health(empty_client)
    assert health.mounting is False
    assert health.system_loaded is True
    await empty_client.post("/api/runtime/unload")


async def test_a_package_build_that_never_returns_fails_the_mount_under_the_start_limit(
    empty_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stalled_build(*_args, **_kwargs):
        await asyncio.sleep(30)

    real_start = runtime_holder.start_runtime

    async def start_with_a_short_limit(build_step, **kwargs):
        kwargs["settings"] = dataclasses.replace(kwargs["settings"], start_timeout=0.05)
        return await real_start(build_step, **kwargs)

    monkeypatch.setattr(runtime_holder, "build_system_from_package", stalled_build)
    monkeypatch.setattr(runtime_holder, "start_runtime", start_with_a_short_limit)
    write_package()

    resp = await asyncio.wait_for(empty_client.post("/api/runtime/mount-package", json=_MOUNT), timeout=5.0)

    assert resp.status_code == 400
    assert "did not build and start within" in resp.json()["detail"]["message"]
    health = await _health(empty_client)
    assert (health.mounting, health.system_loaded) == (False, False)


async def test_the_status_read_says_why_the_last_mount_failed_until_a_mount_succeeds(
    empty_client: AsyncClient,
) -> None:
    write_package(topology=_UNKNOWN_TEACHPOINT, workflows={})
    assert (await empty_client.post("/api/runtime/mount-package", json=_MOUNT)).status_code == 400

    failed = (await empty_client.get("/api/runtime/status")).json()

    assert failed["built"] is False
    assert failed["last_build_error"]["type"] == "ValueError"
    assert "'nowhere'" in failed["last_build_error"]["message"]

    write_package()
    assert (await empty_client.post("/api/runtime/mount-package", json=_MOUNT)).status_code == 200
    assert (await empty_client.get("/api/runtime/status")).json()["last_build_error"] is None
    await empty_client.post("/api/runtime/unload")

async def test_an_unload_tells_every_client_to_run_nothing() -> None:
    app = create_app()
    write_package(workflows=SIMPLE_WORKFLOW)
    async with started_daemon(app) as c:
        assert (await c.post("/api/runtime/mount-package", json=_MOUNT)).status_code == 200
        await c.post("/api/runtime/unload")

    daemon: DaemonState = app.state.daemon
    assignment = daemon.connections.gateway.assignments.for_client("builtin")
    assert assignment is not None and assignment.devices == []


async def test_a_failed_mount_is_no_longer_reported_in_progress(
    empty_client: AsyncClient,
) -> None:
    write_package(topology=_FAILS_AT_START, workflows={})

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 400
    assert (await _health(empty_client)).mounting is False


async def test_the_event_stream_is_registered_before_the_runtime_starts(
    empty_client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_package(workflows=SIMPLE_WORKFLOW)
    calls: list[str] = []
    register_sink = SystemRuntime.register_sink
    start = SystemRuntime.start

    def recording_register_sink(self: SystemRuntime, sink: IEventSink) -> None:
        calls.append(type(sink).__name__)
        register_sink(self, sink)

    async def recording_start(self: SystemRuntime, *, after_reload: bool = False) -> None:
        calls.append("start")
        await start(self, after_reload=after_reload)

    monkeypatch.setattr(SystemRuntime, "register_sink", recording_register_sink)
    monkeypatch.setattr(SystemRuntime, "start", recording_start)

    resp = await empty_client.post("/api/runtime/mount-package", json=_MOUNT)

    assert resp.status_code == 200, resp.text
    assert calls.index("ServerEvents") < calls.index("start")
    await empty_client.post("/api/runtime/unload")


async def test_unload_is_refused_while_a_run_is_active(empty_client: AsyncClient) -> None:
    write_package(workflows=SLOW_WORKFLOW)
    await empty_client.post("/api/runtime/mount-package", json=_MOUNT)
    run = await empty_client.post(
        "/api/executions", json={"workflow_name": "slow_window_workflow", "run_mode": "CHECK_RUN"},
    )
    assert run.status_code == 200, run.text

    resp = await empty_client.post("/api/runtime/unload")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "active_executions_exist"
    assert (await _health(empty_client)).system_loaded is True
