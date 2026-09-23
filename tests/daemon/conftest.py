"""Shared fixtures for `tests/daemon/`.

- `runtime`: a started `SystemRuntime` against the minimal one-device-one-workflow
  test system already used by `test_system_runtime.py`.
- `client`: an httpx AsyncClient that speaks to `create_app(runtime)` in-process
  via ASGI transport. No sockets, no subprocess -- route handlers share the
  test's event loop with the runtime.
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orca.daemon.app import create_app
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[SystemRuntime]:
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system)
    await rt.start()
    try:
        yield rt
    finally:
        # shutdown tolerates being called after an earlier shutdown in a test.
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


@pytest_asyncio.fixture
async def client(runtime: SystemRuntime) -> AsyncIterator[AsyncClient]:
    """Pre-loaded client: bypasses mount by injecting the runtime directly.

    Used by route tests that exercise the loaded-system surface. For tests
    of the mount/load lifecycle itself or of the no-system-loaded state, use
    `empty_client`.
    """
    app = create_app(initial_system_runtime=runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def empty_client() -> AsyncIterator[AsyncClient]:
    """Daemon with no system loaded -- the state right after `orca start`
    but before `orca topology mount`. Used to test the 409 gating on
    execution routes and the mount/load lifecycle.
    """
    app = create_app(initial_system_runtime=None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://daemon.test",
    ) as c:
        yield c
