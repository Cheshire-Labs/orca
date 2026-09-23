"""Shared fixtures for `tests/operations/`.

Reuses the daemon `runtime` fixture for integration tests that exercise
the bound endpoints end-to-end.
"""

from collections.abc import AsyncIterator

import pytest_asyncio

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
        if rt.state.name == "RUNNING":
            await rt.shutdown(confirm=True)
