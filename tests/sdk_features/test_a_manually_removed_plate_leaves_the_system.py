"""A `MANUAL_REMOVE` end in a SIM run takes the plate out of the system at its end spot; its record stays readable."""

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.spawn import MANUAL_REMOVE
from tests.sdk_features.device_world import shake_once
from tests.sdk_features.kit import (
    build_world,
    close_world,
    labware_of,
    new_lab,
    plate_record,
    submit,
    wait_for_the_end,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

SIM = WorkflowRunMode.SIM


async def test_manual_remove_in_sim_takes_the_plate_out_of_the_system_at_the_end() -> None:
    lab = new_lab()
    world = await build_world(lab, shake_once(lab, "pad_1", ("pad_2", MANUAL_REMOVE)))
    try:
        execution_id = await submit(world, mode=SIM)
        ended = await wait_for_the_end(world, execution_id)

        assert set(ended.values()) == {"COMPLETED"}, ended
        [plate_id] = labware_of(world, execution_id, "plate")
        record = await plate_record(world, plate_id)
        assert record.history[-1] == "pad_2"
        assert plate_id not in [s.id for s in await world.runtime.labware.list_all()]
    finally:
        await close_world(world)
