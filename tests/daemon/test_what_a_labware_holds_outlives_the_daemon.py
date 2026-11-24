"""What a deck resident holds survives an unload and a daemon restart."""

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from orca.daemon.app import create_app
from tests.daemon.started_daemon import started_daemon
from orca.resource_models.labware import LabwareInstance
from orca.runtime.system_runtime import SystemRuntime
from orca.state.provenance import Provenance

_PACKAGE = Path(__file__).resolve().parents[2] / "examples" / "hamilton_smc"


async def _mounted_with_workflow(client: AsyncClient, app: FastAPI) -> SystemRuntime:
    (await client.post("/api/runtime/mount-package", json={})).raise_for_status()
    return app.state.daemon.holder.runtime


def _labware(runtime: SystemRuntime, labware_id: str) -> LabwareInstance:
    return next(lw for lw in runtime.system.labwares if lw.id == labware_id)


async def _assert_reads_the_recorded_volume_as_stale(runtime: SystemRuntime, labware_id: str) -> None:
    reservoir = _labware(runtime, labware_id)
    assert reservoir.has_plr_backing
    contents = await runtime.system.labware_contents.of(reservoir.ref)
    assert contents.volumes["A1"] == 40.0
    assert contents.provenance is Provenance.STALE


@pytest.mark.slow
async def test_a_part_used_deck_resident_reads_the_same_and_stale_after_an_unload_and_a_restart(
    tmp_path: Path,
) -> None:
    package = tmp_path / "hamilton_smc"
    shutil.copytree(_PACKAGE, package)
    app = create_app(package_root=package)
    left_on_the_deck = LabwareInstance("bead_reservoir", "hamilton_1_trough_200ml_Vb")

    async with started_daemon(app) as client:
        activity = app.state.daemon.activity
        await activity.labware.register(left_on_the_deck)
        await activity.labware.update_location(left_on_the_deck.id, "mlstar_1/carrier-25-0")
        runtime = await _mounted_with_workflow(client, app)
        await runtime.system.labware_contents.assert_volumes(left_on_the_deck.ref, {"A1": 40.0})
        (await client.post("/api/runtime/unload", json={})).raise_for_status()

        runtime = await _mounted_with_workflow(client, app)
        await _assert_reads_the_recorded_volume_as_stale(runtime, left_on_the_deck.id)
        (await client.post("/api/runtime/unload", json={})).raise_for_status()

    next_daemon = create_app(package_root=package)
    async with started_daemon(next_daemon) as client:
        runtime = await _mounted_with_workflow(client, next_daemon)
        await _assert_reads_the_recorded_volume_as_stale(runtime, left_on_the_deck.id)
        (await client.post("/api/runtime/unload", json={})).raise_for_status()
