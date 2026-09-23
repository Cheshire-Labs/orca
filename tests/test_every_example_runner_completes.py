"""Every example's own entry point runs to completion in simulation.

The other example tests import each example's `build_*` function and never
call its runner. So runners that passed a bool where a run mode goes (and ran
LIVE), submitted no run mode, or asserted two STANDALONE submissions shared an
execution all shipped while every test stayed green. These call the runner
itself, from a directory that is not the repo root, as a reader would.
"""

import importlib
import pathlib
import re
from collections.abc import Awaitable, Callable

import pytest

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


async def test_the_venus_example_tells_the_operator_which_site_each_plate_goes_to(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its transporter is a person: every pick and place prints a prompt and waits for Enter."""
    prompts: list[str] = []

    def confirm(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", confirm)
    await _run("examples.simple_venus_example.simple_venus_example", "run", (True,))

    picks = [re.search(r"PICK UP .* from '(.+)'", p) for p in prompts[0::2]]
    places = [re.search(r"PLACE .* at '(.+)'", p) for p in prompts[1::2]]
    moves = [(pick.group(1), place.group(1)) for pick, place in zip(picks, places) if pick and place]
    assert len(moves) * 2 == len(prompts), prompts
    assert ("plate_pad_1", "ml_star_position_1/sample_site") in moves, moves
    assert ("plate_pad_3", "ml_star_position_1/transfer_site") in moves, moves
    assert all(place != "ml_star_position_1" for _, place in moves), moves
