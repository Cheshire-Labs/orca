"""Every example's own entry point runs to completion in simulation.

The other example tests import each example's `build_*` function and never
call its runner. So runners that passed a bool where a run mode goes (and ran
LIVE), submitted no run mode, or asserted two STANDALONE submissions shared an
execution all shipped while every test stayed green. These call the runner
itself, from a directory that is not the repo root, as a reader would.
"""

import importlib
import pathlib
from collections.abc import Awaitable, Callable

import pytest
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.transporter_models import PickAtCoordsRequest, PlaceAtCoordsRequest

pytest.importorskip("pylabrobot", reason="pylabrobot not installed")


async def _run(module: str, entry: str, args: tuple[int, ...]) -> None:
    runner: Callable[..., Awaitable[None]] = getattr(importlib.import_module(module), entry)
    await runner(*args)


@pytest.mark.parametrize(
    ("module", "entry", "args"),
    [
        ("examples.volume_tracking_example", "main", ()),
        ("examples.multi_lineage.multi_lineage_example", "main", (1,)),
        ("examples.pylabrobot_example.pylabrobot_example", "run", (True,)),
    ],
)
async def test_a_quick_example_runs_to_completion(
    module: str, entry: str, args: tuple[int, ...],
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    await _run(module, entry, args)


# The Hamilton and Opentrons assay tests already run those two workflows end to
# end, so their runners are not run again here.
@pytest.mark.slow
@pytest.mark.timeout(900)
async def test_the_smc_example_runs_to_completion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    await _run("examples.smc_assay.smc_assay_example", "run", (True,))


async def test_the_venus_example_moves_each_plate_to_its_site(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
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
    await _run("examples.simple_venus_example.simple_venus_example", "run", (True,))

    moves = list(zip(picks, places))
    assert len(picks) == len(places), (picks, places)
    assert ("plate_pad_1", "ml_star_position_1/sample_site") in moves, moves
    assert ("plate_pad_3", "ml_star_position_1/transfer_site") in moves, moves
    assert all(place != "ml_star_position_1" for _, place in moves), moves
