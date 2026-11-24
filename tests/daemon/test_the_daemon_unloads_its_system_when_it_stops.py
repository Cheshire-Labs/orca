"""A daemon stopped by a signal or Ctrl-C unloads its mounted system, as `POST /shutdown` does."""

import pytest

from orca.daemon.app import create_app
from orca.daemon.daemon_state import DaemonState
from orca.runtime.runtime_state import RuntimeState
from tests.daemon.fixture_package import write_package
from tests.daemon.started_daemon import started_daemon

pytestmark = pytest.mark.positions_bound_by_the_build


async def test_a_mounted_system_is_shut_down_when_the_daemon_stops() -> None:
    write_package()
    app = create_app()
    daemon: DaemonState = app.state.daemon
    async with started_daemon(app) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        runtime = daemon.holder.built_runtime
        assert runtime is not None

    assert daemon.holder.built_runtime is None
    assert runtime.state is RuntimeState.STOPPED
