"""Shared fixtures for `tests/operations/`.

Reuses the daemon `runtime` fixture for integration tests that exercise
the bound endpoints end-to-end.
"""

from collections.abc import AsyncIterator

import pytest_asyncio

from orca.daemon.app import InjectedSystem
from orca.runtime.system_runtime import SystemRuntime
from tests.test_system_runtime import _build_simple_system
from tests.memory_activity import memory_daemon_system


@pytest_asyncio.fixture
async def injected() -> AsyncIterator[InjectedSystem]:
    system, _ = await _build_simple_system()
    started = await memory_daemon_system(system)
    rt = started.runtime
    await rt.start()
    try:
        yield started
    finally:
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)


@pytest_asyncio.fixture
async def runtime(injected: InjectedSystem) -> SystemRuntime:
    return injected.runtime
