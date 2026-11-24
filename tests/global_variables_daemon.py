"""A daemon app whose system was built over a package that declares global variables in `global_variables.json`."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

import orca.orca as orca
from orca.daemon.app import create_app
from orca.events.event_bus import EventBus
from orca.sdk.build import Topology
from tests.memory_activity import memory_daemon_system, memory_ops_store
from tests.setup_package import package_stores

PLATE_COUNT = '{"plate_count": {"type": "int", "default": 1, "min": 1, "max": 4}}'
"""`plate_count` is declared: an int, default 1, from 1 to 4."""


@asynccontextmanager
async def a_daemon_declaring(global_variables: str) -> AsyncIterator[FastAPI]:
    stores = package_stores()
    (stores.root / "global_variables.json").write_text(global_variables, encoding="utf-8")
    build = await orca.build_system(
        name="globals", topology=Topology(locations={}, transporters=[], pools=[]), stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    injected = await memory_daemon_system(build.system)
    await injected.runtime.start()
    try:
        yield create_app(injected=injected)
    finally:
        await injected.runtime.shutdown(confirm=True)
