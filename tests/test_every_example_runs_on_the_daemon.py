"""Every example runs to completion on the daemon, the way a user runs it.

Each example folder is copied to a temp folder, so a run's `.orca/` state
never lands in the repo and every run starts clean. The daemon builds the copy
as its package folder, and the example's workflow runs in CHECK_RUN.

The example list is discovered, so a new example is covered without anyone
remembering this file exists.
"""

import asyncio
import shutil
import time
from pathlib import Path

import pytest
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.transporter_models import PickAtCoordsRequest, PlaceAtCoordsRequest
from httpx import AsyncClient
from pydantic import JsonValue

from orca.daemon.app import create_app
from tests.daemon.started_daemon import started_daemon

pytest.importorskip("pylabrobot", reason="pylabrobot not installed")

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
# The assays run hundreds of steps; they belong in the slow job.
_SLOW = frozenset({"smc_assay", "hamilton_smc", "opentrons_flex_smc"})
_TERMINAL = frozenset({"completed", "partial_completed", "failed", "aborted"})


def _example_packages() -> list[str]:
    return sorted(path.parent.name for path in _EXAMPLES.glob("*/topology.py"))


def _sample_groups(count: int) -> list[JsonValue]:
    return [
        {
            "id": f"group_{index}",
            "members": [{"thread_template_name": "sample", "acquisition": {"kind": "pool"}}],
        }
        for index in range(count)
    ]


def _submission(package: str, workflow_name: str) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {"workflow_name": workflow_name, "run_mode": "CHECK_RUN"}
    if package == "multi_lineage":
        # Several lineages feed one shared reservoir; one group would not exercise that.
        body["groups"] = _sample_groups(3)
    return body


async def _run_to_the_end(client: AsyncClient, body: dict[str, JsonValue], timeout: float) -> dict[str, JsonValue]:
    submitted = await client.post("/api/executions", json=body)
    assert submitted.status_code == 200, submitted.text
    execution_id = submitted.json()["execution_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await client.get(f"/api/executions/{execution_id}")
        assert detail.status_code == 200, detail.text
        if detail.json()["status"] in _TERMINAL:
            return detail.json()
        await asyncio.sleep(0.5)
    raise AssertionError(f"{body['workflow_name']} did not finish within {timeout}s")


async def _state_every_liquid_handler_head_empty(client: AsyncClient) -> None:
    """What a user does before a vendor protocol: state that each liquid handler's head carries nothing."""
    topology = await client.get("/api/topology")
    assert topology.status_code == 200, topology.text
    for device in topology.json()["devices"]:
        if device["declared_kind"] == "liquid_handler":
            stated = await client.put(f"/api/liquid-handlers/{device['name']}/mounted-tips", json={"mounted": []})
            assert stated.status_code == 200, stated.text


async def _run_example(package: str, tmp_path: Path, timeout: float) -> None:
    folder = tmp_path / package
    shutil.copytree(_EXAMPLES / package, folder)
    app = create_app(package_root=folder)
    async with started_daemon(app) as client:
        mounted = await client.post("/api/runtime/mount-package", json={})
        assert mounted.status_code == 200, mounted.text
        await _state_every_liquid_handler_head_empty(client)
        try:
            listing = await client.get("/api/workflows")
            workflows = [w["name"] for w in listing.json()["workflows"]]
            assert workflows, f"{package} has no workflow to run"
            for workflow_name in workflows:
                detail = await _run_to_the_end(
                    client, _submission(package, workflow_name), timeout,
                )
                assert detail["status"] == "completed", (package, workflow_name, detail["error"])
        finally:
            await client.post("/api/runtime/unload", json={})


def test_the_discovery_finds_the_examples() -> None:
    """Without this, an empty list would make every case below vacuous."""
    found = _example_packages()
    assert len(found) >= 6, found
    assert "smc_assay" in found


@pytest.mark.parametrize("package", sorted(set(_example_packages()) - _SLOW))
async def test_a_quick_example_runs_to_completion_on_the_daemon(
    package: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not the repo root: an example that spells a path relative to the shell
    # has to fail here rather than pass by where pytest happened to start.
    monkeypatch.chdir(tmp_path)
    await _run_example(package, tmp_path, timeout=100.0)


@pytest.mark.slow
@pytest.mark.timeout(1800)
@pytest.mark.parametrize("package", sorted(_SLOW))
async def test_an_assay_example_runs_to_completion_on_the_daemon(
    package: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    await _run_example(package, tmp_path, timeout=1500.0)


async def test_the_venus_example_moves_each_plate_to_its_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its transporter is a person; in simulation each pick and place still names the site."""
    picks: list[str] = []
    places: list[str] = []
    pick = SimTransporterDriver.pick_at_coords
    place = SimTransporterDriver.place_at_coords

    async def record_pick(self: SimTransporterDriver, request: PickAtCoordsRequest) -> None:
        picks.append(request.teachpoint.position_id)
        await pick(self, request)

    async def record_place(self: SimTransporterDriver, request: PlaceAtCoordsRequest) -> None:
        places.append(request.teachpoint.position_id)
        await place(self, request)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SimTransporterDriver, "pick_at_coords", record_pick)
    monkeypatch.setattr(SimTransporterDriver, "place_at_coords", record_place)
    await _run_example("simple_venus_example", tmp_path, timeout=100.0)

    moves = list(zip(picks, places))
    assert len(picks) == len(places), (picks, places)
    assert ("plate_pad_1", "ml_star_position_1/sample_site") in moves, moves
    assert ("plate_pad_3", "ml_star_position_1/transfer_site") in moves, moves
    assert all(place != "ml_star_position_1" for _, place in moves), moves


async def test_a_check_run_sees_the_head_the_operator_stated_on_the_real_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check run runs on a copy of the record made at its start; the copy was never unwatched."""
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "simple_venus_example"
    shutil.copytree(_EXAMPLES / "simple_venus_example", folder)
    app = create_app(package_root=folder)
    async with started_daemon(app) as client:
        mounted = await client.post("/api/runtime/mount-package", json={"sim": False})
        assert mounted.status_code == 200, mounted.text
        try:
            await _state_every_liquid_handler_head_empty(client)
            detail = await _run_to_the_end(
                client, {"workflow_name": "example_workflow", "run_mode": "CHECK_RUN"}, timeout=100.0,
            )
            assert detail["status"] == "completed", detail["error"]
        finally:
            await client.post("/api/runtime/unload", json={})
