import logging
import os
from collections.abc import AsyncGenerator, Generator
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio

from orca.gateway.controller.command_pipeline import DeviceCommandPipeline
from orca.daemon.lifecycle import DaemonInfo
from orca.resource_models.position_occupancy import PositionOccupancy
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode, current_run_mode
from orca.runtime.store_factory import default_package_root
from orca.system.registries import LabwareRegistry
from orca.resource_models.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.method_template import drain_pending_method_templates
from orca.workflow_models.thread_template import drain_pending_thread_templates
from tests.test_helpers import create_test_transporter, create_test_device, wire_system_map
from tests.memory_activity import close_memory_activity
from tests.placements_for_tests import fresh_test_placements
from tests.audit_capture import AuditCapture, capture_audit
from tests.isolated_daemon import START_LIMIT_S, start_isolated_daemon, stop_isolated_daemon
from tests.real_device_path import RealDevicePath, real_device_path

os.environ.setdefault("ORCA_SIM_SPEEDUP", "off")


@pytest_asyncio.fixture(autouse=True)
async def _close_memory_activity() -> AsyncGenerator[None, None]:
    yield
    await close_memory_activity()


@pytest.fixture
def audit() -> Generator[AuditCapture, None, None]:
    """The audit rows written during the test, read back through IAuditStore."""
    with capture_audit() as capture:
        yield capture


@pytest.fixture(autouse=True)
def _an_empty_world_for_positions_no_build_binds(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unit test that builds no system still needs a placement ledger, so each test gets an empty one.

    A system build binds its own ledger over this one. Mark a test
    `positions_bound_by_the_build` to prove the build reached every position.
    """
    if request.node.get_closest_marker("positions_bound_by_the_build") is not None:
        return
    placements = fresh_test_placements()
    unbound_init = PositionOccupancy.__init__

    def _bound_init(self: PositionOccupancy, position_id: str, *, single_occupant: bool = True) -> None:
        unbound_init(self, position_id, single_occupant=single_occupant)
        self.bind_placements(placements)

    monkeypatch.setattr(PositionOccupancy, "__init__", _bound_init)


LOG_DIR = Path(__file__).parent / "logs"


@pytest.fixture(autouse=True)
def _seed_run_mode_for_tests(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Seed `current_run_mode` for every test (sim-hierarchy v3.4).

    Production seeds via SystemRuntime._run_workflow() /
    ExecutingLabwareThread.start(). Unit tests that construct Devices or
    Transporters directly and call them without going through those seam
    points still need a seeded ContextVar so `device.driver` dispatches.
    CHECK_RUN is the right default for tests: every device wrapper returns
    its sim driver.
    """
    if request.node.get_closest_marker("out_of_run") is not None:
        # An operator's command outside any run: nothing seeded, as a server request sees it.
        yield
        return
    token = current_run_mode.set(WorkflowRunMode.CHECK_RUN)
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

_TEST_BODY_LIMIT_S = 120.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """The per-test time limit covers fixture setup, so a test that starts a daemon gets the start limit on top."""
    for item in items:
        if "running_daemon" in getattr(item, "fixturenames", ()) and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(START_LIMIT_S + _TEST_BODY_LIMIT_S))


@pytest.fixture
def running_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Generator[DaemonInfo, None, None]:
    """Live daemon in an isolated ORCA_DAEMON_HOME. No system loaded yet.

    ORCA_DAEMON_HOME is monkeypatched so OrcaClient's `pid_file_path()` finds this daemon.
    """
    monkeypatch.setenv("ORCA_DAEMON_HOME", str(tmp_path))
    daemon = start_isolated_daemon(tmp_path, default_package_root())
    try:
        yield daemon.info
    finally:
        stop_isolated_daemon(daemon)


@pytest.fixture
def loaded_daemon(running_daemon: DaemonInfo) -> DaemonInfo:
    """A running daemon with its package folder mounted in sim mode.

    The folder holds the fixture topology plus `simple_workflow` and
    `slow_window_workflow`. Tests can exercise the whole CLI -> daemon -> runtime path.
    """
    import httpx
    from tests.daemon.fixture_package import SIMPLE_WORKFLOW, SLOW_WORKFLOW, write_package

    write_package(workflows={**SIMPLE_WORKFLOW, **SLOW_WORKFLOW})
    info = running_daemon
    with httpx.Client(timeout=30.0) as c:
        mount = c.post(f"http://127.0.0.1:{info.port}/api/runtime/mount-package", json={})
        assert mount.status_code == 200, (
            f"POST /api/runtime/mount-package failed: {mount.status_code} {mount.text}"
        )
    return info




async def _save_nowhere(command: str) -> None:
    return None

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
    # This map is in no system, so a world command a mover runs has no history to be saved in.
    for robot in (robot1, robot2):
        robot.engine_commands.save_with(_save_nowhere, DeviceCommandPipeline(()))

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

@pytest.fixture(autouse=True)
def _setup_files_in_a_temp_package(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon keeps setup files in its package folder; never let a test's be the repo."""
    monkeypatch.setenv("ORCA_PACKAGE_DIR", str(tmp_path_factory.mktemp("package")))


@pytest_asyncio.fixture(autouse=True)
async def _devices_on_the_real_device_path(
    request: pytest.FixtureRequest, _setup_files_in_a_temp_package: None,
) -> AsyncGenerator[RealDevicePath, None]:
    """Every device a test builds goes through the check run's controller and in-process orca-client."""
    async with real_device_path(run_mode=_parametrized_run_mode(request)) as path:
        yield path


def _parametrized_run_mode(request: pytest.FixtureRequest) -> WorkflowRunMode | None:
    """The run mode a test case is parametrized with, if any."""
    node = request.node
    if not isinstance(node, pytest.Function) or not hasattr(node, "callspec"):
        return None
    return next((value for value in node.callspec.params.values() if isinstance(value, WorkflowRunMode)), None)
