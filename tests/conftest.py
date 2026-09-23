import logging
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio

from orca.daemon.lifecycle import DaemonInfo
from orca.resource_models.transporter import Transporter
from orca.runtime.run_modes import WorkflowRunMode, current_run_mode
from orca.system.registries import LabwareRegistry
from orca.system.resource_registry import ResourceRegistry
from orca.resource_models.labware_directory import reset_labware_directory
from orca.state.current import reset_placement_ledger
from orca.system.system_map import SystemMap
from orca.workflow_models.method_template import drain_pending_method_templates
from orca.workflow_models.thread_template import drain_pending_thread_templates
from tests.test_helpers import create_test_transporter, create_test_device, wire_system_map

@pytest.fixture(autouse=True)
def _empty_world() -> Generator[None, None, None]:
    """One placement ledger per process means one world; each test starts in an
    empty one."""
    reset_placement_ledger()
    reset_labware_directory()
    yield
    reset_placement_ledger()
    reset_labware_directory()


LOG_DIR = Path(__file__).parent / "logs"


@pytest.fixture(autouse=True)
def _seed_run_mode_for_tests() -> Generator[None, None, None]:
    """Seed `current_run_mode` for every test (sim-hierarchy v3.4).

    Production seeds via SystemRuntime.start() / WorkflowExecutor.start() /
    ExecutingLabwareThread.start(). Unit tests that construct Devices or
    Transporters directly and call them without going through those seam
    points still need a seeded ContextVar so `device.driver` dispatches.
    PURE_SIM is the right default for tests: every device wrapper returns
    its sim driver.
    """
    token = current_run_mode.set(WorkflowRunMode.PURE_SIM)
    try:
        yield
    finally:
        current_run_mode.reset(token)


@pytest.fixture(autouse=True)
def _clear_pending_template_lists() -> Generator[None, None, None]:
    """Reset the module-level pending-template lists between tests.

    `@orca.method` / `@orca.thread` decorators append to module-level
    `_PENDING_*` lists (drained by `SdkToSystemBuilder`). When tests import
    modules that define decorated templates, those entries persist across
    tests unless we drain. Flush before + after every test to keep tests
    order-independent.
    """
    drain_pending_method_templates()
    drain_pending_thread_templates()
    yield
    drain_pending_method_templates()
    drain_pending_thread_templates()


@pytest.fixture(autouse=True, scope="session")
def _configure_orca_logging() -> Generator[None, None, None]:
    """Route orca.* and orca.events logs to tests/logs/ for all tests."""
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    handler = logging.FileHandler(LOG_DIR / f"smc-{timestamp}.log", mode="w")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

    orca_logger = logging.getLogger("orca")
    orca_logger.addHandler(handler)
    orca_logger.setLevel(logging.INFO)

    yield

    orca_logger.removeHandler(handler)
    handler.close()


# -- Daemon-backed fixtures (for CLI tests that need a real running daemon) --


def _pick_free_port_for_daemon() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_isolated_daemon(port: int, pid_dir: Path) -> object:
    """Spawn `python -m orca.daemon` with ORCA_DAEMON_HOME pointed at pid_dir.

    Must also inject PYTHONPATH so the subprocess can import
    `tests.daemon.daemon_test_fixture_topology` for mount/load specs.
    """
    import os
    import subprocess
    import sys

    project_root = str(Path(__file__).resolve().parent.parent)
    env = {
        **os.environ,
        "ORCA_DAEMON_HOME": str(pid_dir),
        "PYTHONPATH": project_root + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    cmd = [sys.executable, "-m", "orca.daemon", "--port", str(port)]
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
            env=env,
        )
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )


def _wait_for_daemon_health(port: int, timeout_s: float = 45.0) -> bool:
    import time
    import httpx

    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    with httpx.Client(timeout=1.0) as client:
        while time.time() < deadline:
            try:
                if client.get(url).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    return False


@pytest.fixture
def running_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Generator[DaemonInfo, None, None]:
    """Live daemon in an isolated ORCA_DAEMON_HOME. No system loaded yet.

    Yields the DaemonInfo read from the subprocess-written PID file. The
    in-process test can then call CLI commands (CliRunner) or HTTP calls --
    ORCA_DAEMON_HOME is monkeypatched so LocalDaemonClient's `pid_file_path()`
    returns the same tmp path.
    """
    import time
    import httpx
    import psutil

    from orca.daemon.lifecycle import read_pid_file

    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))

    port = _pick_free_port_for_daemon()
    proc = _spawn_isolated_daemon(port, tmp_path)
    daemon_pid: int | None = None
    try:
        if not _wait_for_daemon_health(port):
            raise RuntimeError(
                f"daemon did not start within the health-wait budget "
                f"(port={port}, see {tmp_path}/daemon.log)",
            )
        info = read_pid_file(tmp_path / "daemon.json")
        assert info is not None, "daemon did not write a PID file"
        daemon_pid = info.pid
        yield info
    finally:
        # Shutdown.
        try:
            with httpx.Client(timeout=5.0) as c:
                c.post(f"http://127.0.0.1:{port}/shutdown")
        except httpx.HTTPError:
            pass
        if daemon_pid is not None:
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if not psutil.pid_exists(daemon_pid):
                    break
                time.sleep(0.1)
            if psutil.pid_exists(daemon_pid):
                try:
                    psutil.Process(daemon_pid).terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass


@pytest.fixture
def loaded_daemon(running_daemon: DaemonInfo) -> DaemonInfo:
    """A running daemon with the topology mounted + `simple_workflow` loaded
    in sim mode. Tests can exercise the whole CLI -> daemon -> runtime path.

    Mirrors the split ingress: mount the topology, then register
    the workflow separately.
    """
    import httpx
    info = running_daemon
    port = info.port
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(timeout=30.0) as c:
        mount = c.post(
            f"{base}/mount-topology",
            json={
                "spec": "tests.daemon.daemon_test_fixture_topology:build_topology",
                "sim": True,
            },
        )
        assert mount.status_code == 200, (
            f"POST /mount-topology failed: {mount.status_code} {mount.text}"
        )
        load = c.post(
            f"{base}/workflows",
            json={
                "spec": "tests.daemon.daemon_test_fixture_topology:build_workflow",
            },
        )
        assert load.status_code == 200, (
            f"POST /workflows failed: {load.status_code} {load.text}"
        )
    return info


@pytest_asyncio.fixture
async def system_map() -> SystemMap:
    """
    Create a system map for test_graph.py tests.
    Recreates the graph structure from the original fixture.
    """
    # Create robots with teachpoints matching original test expectations
    robot1 = create_test_transporter("robot1", ["loc1", "loc2", "loc3", "stacker1", "shaker1"])
    robot2 = create_test_transporter("robot2", ["loc3", "loc4", "loc5", "ham1"])

    # Create devices
    stacker1 = create_test_device("stacker1")
    shaker1 = create_test_device("shaker1")
    ham1 = create_test_device("ham1")

    # Create registry and add resources
    registry = ResourceRegistry()
    registry.add_resource(robot1)
    registry.add_resource(robot2)
    registry.add_resource(stacker1)
    registry.add_resource(shaker1)
    registry.add_resource(ham1)

    # Flat-model wiring order: pads + device sites, then edges.
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"stacker1": stacker1, "shaker1": shaker1, "ham1": ham1},
        pads=["loc1", "loc2", "loc3", "loc4", "loc5"],
    )
    return system_map