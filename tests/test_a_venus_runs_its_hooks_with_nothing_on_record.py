"""A Venus pick or place with no plate on record still runs its hook methods, with the site and no labware named."""

import pytest

from orca.devices.venus import Venus
from orca.resource_models.transporter import Transporter

from tests.real_device_path import the_real_device_path
from tests.test_a_venus_runs_live_and_tracks_what_it_declares import (
    _build,
    _FakeHamiltonPc,
    hamilton_pc,
)

__all__ = ["hamilton_pc"]

pytestmark = pytest.mark.asyncio


async def test_each_hook_runs_on_the_hamilton_pc_with_the_site_and_no_labware(hamilton_pc: _FakeHamiltonPc) -> None:
    build = await _build(hamilton_pc)
    await the_real_device_path().connect_system(build.system)
    venus = build.system.get_device("ml_star")
    arm = next(m for m in build.system.movers if m.name == "arm")
    assert isinstance(venus, Venus) and isinstance(arm, Transporter)
    await venus.initialize()

    await venus._do_prepare_for_pick(None, arm, site="sample_site")
    await venus._do_notify_picked(None, arm, site="sample_site")
    await venus._do_prepare_for_place(None, arm, site="sample_site")
    await venus._do_notify_placed(None, arm, site="sample_site")

    calls = hamilton_pc.runs()
    assert [c.method for c in calls] == ["PrepPick.hsl", "Picked.hsl", "PrepPlace.hsl", "Placed.hsl"]
    for call in calls:
        assert call.params["site"] == "sample_site"
        assert call.params["labware_name"] == "" and call.params["labware_type"] == ""
