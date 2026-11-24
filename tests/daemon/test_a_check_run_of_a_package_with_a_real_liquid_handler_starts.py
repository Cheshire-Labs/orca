"""A check run of a package whose liquid handler has a real STAR or OT2 driver starts and runs to the end.

The check run runs every device on its simulator, so it must not be refused because the simulator
implements fewer interfaces than the real driver the setup file names.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import JsonValue

from orca.daemon.app import create_app
from tests.daemon.liquid_handler_package import write_liquid_handler_package
from tests.daemon.started_daemon import started_daemon

_TERMINAL = frozenset({"completed", "partial_completed", "failed", "aborted"})

_REAL_SETUPS: dict[str, dict[str, JsonValue]] = {
    "no real driver": {},
    "STAR": {"client": "lab_pc", "real": {"type": "plr", "backend": "STAR"}},
    "OT2": {
        "client": "lab_pc",
        "real": {"type": "plr", "backend": "OpentronsOT2Backend", "connection": {"type": "tcp", "host": "10.0.0.5"}},
    },
}


@asynccontextmanager
async def _daemon(root: Path) -> AsyncIterator[AsyncClient]:
    async with started_daemon(create_app(package_root=root)) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        try:
            yield client
        finally:
            await client.post("/api/runtime/unload")


async def _status_at_the_end(client: AsyncClient, execution_id: str) -> str:
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        detail = await client.get(f"/api/executions/{execution_id}")
        assert detail.status_code == 200, detail.text
        status: str = detail.json()["status"]
        if status in _TERMINAL:
            return status
        await asyncio.sleep(0.2)
    raise AssertionError(f"check run {execution_id} did not finish")


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", sorted(_REAL_SETUPS))
async def test_a_check_run_of_a_package_with_a_real_liquid_handler_starts(driver: str) -> None:
    async with _daemon(write_liquid_handler_package(_REAL_SETUPS[driver])) as client:
        submitted = await client.post(
            "/api/executions", json={"workflow_name": "simple_workflow", "run_mode": "CHECK_RUN"},
        )

        assert submitted.status_code == 200, submitted.text
        assert await _status_at_the_end(client, submitted.json()["execution_id"]) == "completed"
