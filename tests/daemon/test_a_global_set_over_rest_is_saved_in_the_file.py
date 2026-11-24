"""A global set over REST is saved in `global_variables.json` as `value`, beside `default`, and a reload reads it.

The running store takes the value at once; the file keeps it, so a rebuild of the system from the same package
(what a reload does) starts with it. Deleting the value removes only `value`: a declared default stays.
"""
import json
from pathlib import Path

import pytest

import orca.orca as orca
from httpx import ASGITransport, AsyncClient
from orca.events.event_bus import EventBus
from orca.sdk.build import Topology
from orca.runtime.setup_file_stores import GlobalVariableFileStore
from orca.runtime.setup_files import SetupFileError
from orca.variables.errors import OptionValue
from orca.variables.variable_definition import GlobalVariable
from tests.global_variables_daemon import PLATE_COUNT, a_daemon_declaring
from tests.memory_activity import memory_daemon_system, memory_ops_store
from tests.setup_package import package_stores

URL = "/api/variables/global"


def _file() -> dict[str, dict[str, OptionValue]]:
    path: Path = package_stores().root / "global_variables.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def _a_rebuilt_system_reads(name: str) -> OptionValue:
    build = await orca.build_system(
        name="globals", topology=Topology(locations={}, transporters=[], pools=[]), stores=package_stores(),
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    injected = await memory_daemon_system(build.system)
    return injected.runtime.variables.get_global(name)


async def test_a_set_value_is_saved_beside_the_default_and_a_rebuild_reads_it() -> None:
    async with a_daemon_declaring(PLATE_COUNT) as app:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon.test") as client:
            assert (await client.put(f"{URL}/plate_count", json={"value": 3})).status_code == 200
            assert (await client.put(f"{URL}/operator", json={"value": "ana"})).status_code == 200

            assert _file() == {
                "plate_count": {"type": "int", "default": 1, "min": 1, "max": 4, "value": 3},
                "operator": {"value": "ana"},
            }
            assert await _a_rebuilt_system_reads("plate_count") == 3
            assert await _a_rebuilt_system_reads("operator") == "ana"

            assert (await client.delete(f"{URL}/plate_count")).status_code == 200
            assert (await client.delete(f"{URL}/operator")).status_code == 200
            assert _file() == {"plate_count": {"type": "int", "default": 1, "min": 1, "max": 4}}
            assert await _a_rebuilt_system_reads("plate_count") == 1


async def test_a_refused_value_leaves_the_file_as_it_was() -> None:
    async with a_daemon_declaring(PLATE_COUNT) as app:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon.test") as client:
            assert (await client.put(f"{URL}/plate_count", json={"value": 9})).status_code == 422

            assert _file() == {"plate_count": {"type": "int", "default": 1, "min": 1, "max": 4}}


async def test_a_value_the_file_cannot_take_is_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: GlobalVariableFileStore, entries: dict[str, GlobalVariable]) -> None:
        raise SetupFileError(package_stores().root / "global_variables.json", "held open by another program")

    async with a_daemon_declaring(PLATE_COUNT) as app:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://daemon.test") as client:
            monkeypatch.setattr(GlobalVariableFileStore, "write", refuse)
            refused = await client.put(f"{URL}/plate_count", json={"value": 3})
            assert refused.json()["detail"]["code"] == "setup_file_invalid", refused.json()

            assert (await client.get(f"{URL}/plate_count")).json()["value"] == 1
