"""An operator's pick or place where the record has no plate is not refused and uses the device's default grip.
No position moves, and the record names no labware. A run's move
still refuses an empty source."""

from unittest.mock import patch

import pytest

from orca.resource_models.transporter_base import TransporterBase
from orca.state.records import GenericOperationDetails

from tests.test_adhoc_commands_are_recorded import (
    WORKING_SITE,
    _all_ops,
    _Bench,
    _operator_reach_in,
    running_arm_bench,
    running_lh_bench,
)
from tests.test_hand_and_run_commands_match import _run_move

pytestmark = pytest.mark.asyncio

EMPTY_DECK_SITE = "lh/carrier-25-1"


def _mover(bench: _Bench, name: str) -> TransporterBase:
    mover = next(m for m in bench.runtime.system.movers if m.name == name)
    assert isinstance(mover, TransporterBase)
    return mover


async def _nothing() -> None:
    return None


@pytest.mark.timeout(60)
async def test_an_arm_picks_and_places_by_hand_with_nothing_on_record() -> None:
    async with running_arm_bench() as bench:
        before = bench.ledger_site()

        await _operator_reach_in(bench, "arm", "pick_at_coords", "stacker")
        assert _mover(bench, "arm").labware is None, "the jaws record nothing: none was on record"
        await _operator_reach_in(bench, "arm", "place_at_coords", "stacker")

        assert bench.ledger_site() == before
        reaches = [
            (op.details.command, op.affected_labware) for op in await _all_ops(bench)
            if op.device_name == "arm" and isinstance(op.details, GenericOperationDetails)
            and op.details.command in ("pick_at_coords", "place_at_coords")
        ]
        assert reaches == [("pick_at_coords", []), ("place_at_coords", [])], "the record names no labware"


@pytest.mark.timeout(60)
async def test_a_deck_gripper_moves_by_hand_with_its_default_grip_when_nothing_is_on_record() -> None:
    async with running_lh_bench() as bench:
        gripper = f"{bench.lh_name}/gripper"
        driver = bench.runtime.system.get_device(bench.lh_name).driver
        before = bench.ledger_site()

        await _operator_reach_in(bench, gripper, "pick_at_coords", WORKING_SITE)
        with patch.object(driver, "move_plate", wraps=driver.move_plate) as move_plate:
            await _operator_reach_in(bench, gripper, "place_at_coords", EMPTY_DECK_SITE)

        [call] = move_plate.await_args_list
        assert call.args[0].plate is None, "no plate named: the driver grips with its default labware"
        assert bench.ledger_site() == before


@pytest.mark.timeout(60)
async def test_a_runs_move_from_a_site_with_no_plate_on_record_is_still_refused() -> None:
    async with running_arm_bench() as bench:
        await _operator_reach_in(bench, "arm", "pick_at_coords", "pad")
        await _operator_reach_in(bench, "arm", "place_at_coords", "stacker")
        with pytest.raises(Exception, match="is empty"):
            await _run_move(bench, "arm", "pad", "pad", _nothing)


@pytest.mark.timeout(60)
async def test_a_runs_deck_gripper_move_from_an_empty_site_is_still_refused() -> None:
    async with running_lh_bench() as bench:
        with pytest.raises(Exception, match="is empty"):
            await _run_move(bench, f"{bench.lh_name}/gripper", WORKING_SITE, EMPTY_DECK_SITE, _nothing)
