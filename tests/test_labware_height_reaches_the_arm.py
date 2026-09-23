"""The arm is told how tall its load is, instead of looking the height up.

A 99 mm tip rack was picked with a 14.35 mm microplate's height because the
driver resolved the height from a name-keyed catalog and the name missed: the
catalog was keyed by PyLabRobot's factory name while the wire carried the
labware's other name. A miss returns silence, not an error, so the move ran on
plate numbers and the approach came down inside the rack.

The height is a fact about the labware, and the labware knows it. These pin the
whole path, from a real tip rack to the request the arm receives.
"""

from typing import List

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.plr.labware import PLRTipRackAdapter
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.transporter_models import PickAtCoordsRequest
from pylabrobot.resources import flex_96_tiprack_1000ul

from orca.resource_models.labware import TipRackInstance, TipRackTemplate
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.labware_catalog import InMemoryLabwareCatalog
from cheshire_drivers.labware_seed import load_labware_seed

from tests.test_helpers import (
    _SingleDriverFactory,
    create_test_teachpoints,
    seeded_teachpoint_service,
)

pytestmark = pytest.mark.asyncio

TIP_RACK_HEIGHT_MM = 99.0
SEEDED_MICROPLATE_HEIGHT_MM = 14.35


class RecordingArmDriver(SimTransporterDriver):
    """A sim arm that keeps the pick request it was handed.

    Records instead of delegating: the sim's world graph is not seeded here, and
    what this pins is what the arm was TOLD, not whether the sim would allow it.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.picks: List[PickAtCoordsRequest] = []

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        self.picks.append(request)


DECLARED_TYPE = "flex_96_tiprack_1000ul"


def _tip_rack_instance() -> TipRackInstance:
    rack = PLRTipRackAdapter(flex_96_tiprack_1000ul("r4_tips-abc123"))
    return TipRackInstance(
        rack, template_name="r4_tips", labware_type=DECLARED_TYPE,
    )


def _arm(driver: SimTransporterDriver) -> Transporter:
    store = seeded_teachpoint_service(create_test_teachpoints(["pad1"]))
    with use_device_factory(_SingleDriverFactory(driver)):
        return Transporter("robot1", teachpoint_store=store)


class TestTheArmIsToldWhatItIsCarrying:
    async def test_a_pick_carries_the_rack_s_real_height_to_the_driver(self) -> None:
        """The bench failure, end to end: this is the number the retreat is
        computed from, and the one that was a microplate's."""
        driver = RecordingArmDriver("robot1")
        arm = _arm(driver)
        rack = _tip_rack_instance()
        pad = PlatePad("pad1")
        await pad.notify_placed(rack, arm)
        location = Location("pad1", pad)

        await arm._do_pick(location)

        assert driver.picks, "the arm was never asked to pick"
        sent = driver.picks[-1].handling.resource_height
        assert sent == TIP_RACK_HEIGHT_MM, (
            f"the arm was sent {sent} mm for a {TIP_RACK_HEIGHT_MM} mm rack; "
            f"{SEEDED_MICROPLATE_HEIGHT_MM} means the height was resolved by name "
            "somewhere and the lookup missed"
        )

    async def test_a_rack_states_its_own_height(self) -> None:
        assert _tip_rack_instance().size_z == TIP_RACK_HEIGHT_MM

    async def test_a_measured_grip_profile_overrules_the_catalog_height(self) -> None:
        """The labware's own height is the base of the labware layer, not the last
        word: somebody who measured this labware on this arm overrules it."""
        driver = RecordingArmDriver("robot1")
        arm = _arm(driver)
        teachpoint = await arm._resolve_teachpoint("pad1")

        resolved = await arm.resolve_handling(
            teachpoint, "flex_96_tiprack_1000ul",
            MoveParameterPatch(resource_height=101.5),
            labware_height=TIP_RACK_HEIGHT_MM,
        )

        assert resolved.parameters.resource_height == 101.5

    async def test_reaching_a_site_empty_handed_states_no_height(self) -> None:
        """`how does this arm reach that site` is a different question from `how
        will it hold this rack`, and must not answer the second one."""
        driver = RecordingArmDriver("robot1")
        arm = _arm(driver)
        teachpoint = await arm._resolve_teachpoint("pad1")

        resolved = await arm.resolve_handling(teachpoint)

        assert resolved.parameters.resource_height == SEEDED_MICROPLATE_HEIGHT_MM


async def test_an_instance_is_identified_by_what_its_template_declared() -> None:
    """The key every layer looks a labware up by comes from the declaration, never
    from the labware library's own metadata.

    PyLabRobot builds this rack from a factory named `flex_96_tiprack_1000ul`, and
    the object it hands back carries `.model = "opentrons_flex_96_tiprack_1000ul"`,
    Opentrons' load name. `.model` is optional: plenty of labware carries None.
    Keying a grip profile, a per-position override or a catalog row on it means the
    lookup silently misses for anything whose library left it empty, and matches
    under a name nobody wrote for anything that filled it in.
    """
    template = TipRackTemplate("r4_tips", labware_type=DECLARED_TYPE, with_tips=True)
    await template.bind_catalog(InMemoryLabwareCatalog(load_labware_seed()))
    instance = await template.create_instance()

    assert flex_96_tiprack_1000ul("probe").model != DECLARED_TYPE
    assert instance.labware_type == DECLARED_TYPE
