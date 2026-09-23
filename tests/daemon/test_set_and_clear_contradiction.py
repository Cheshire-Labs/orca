"""Naming one field in both halves of a move-parameter edit.

Four surfaces take a `set` / `clear` pair: an arm's defaults, a labware type's
grip profile, one labware at one position, and one piece of labware being
carried. Every one of them merges the halves the same way, so the same
contradiction is expressible on all four and has to be refused on all four.
Refusing on one is worse than refusing on none: the operator learns a rule that
then does not hold next door.
"""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from httpx import AsyncClient

from orca.runtime.move_parameters import (
    MoveParameterEditRefused,
    contested_fields,
    reject_contradiction,
    reject_site_owned,
)


CONTRADICTION = {"set": {"z_offset": 3.0}, "clear": ["z_offset"]}


class TestTheRuleItself:
    def test_it_names_every_contested_field_and_only_those(self) -> None:
        contested = contested_fields(
            MoveParameterPatch(z_offset=3.0, speed=50.0, clearance=2.0),
            ["z_offset", "speed", "jaw_opening"],
        )

        assert contested == ["speed", "z_offset"]

    def test_a_field_the_edit_only_hands_back_is_not_contested(self) -> None:
        """Clearing a field the caller is not also setting is the normal way to
        hand one back, and the commonest edit there is."""
        reject_contradiction(MoveParameterPatch(z_offset=3.0), ["speed"])

    def test_a_field_set_to_none_is_not_a_value_it_is_an_absence(self) -> None:
        """None means inherit, so naming it in `clear` says the same thing
        twice rather than two opposite things."""
        reject_contradiction(MoveParameterPatch(z_offset=None), ["z_offset"])

    def test_saying_both_about_one_field_is_refused(self) -> None:
        with pytest.raises(ValueError, match="z_offset"):
            reject_contradiction(MoveParameterPatch(z_offset=3.0), ["z_offset"])

    def test_the_refusal_carries_the_field_names_rather_than_only_prose(self) -> None:
        """Every surface answering this has to name the fields, and re-deriving
        them from the request is how the two answers drift apart."""
        with pytest.raises(MoveParameterEditRefused) as contradiction:
            reject_contradiction(
                MoveParameterPatch(z_offset=3.0, speed=50.0), ["z_offset", "speed"],
            )
        with pytest.raises(MoveParameterEditRefused) as site_owned:
            reject_site_owned(MoveParameterPatch(clearance=3.0, z_offset=1.0))

        assert contradiction.value.fields == ["speed", "z_offset"]
        assert site_owned.value.fields == ["clearance"], "only the ones at fault"


class TestTheWiderLayers:
    """Both are deployment-scoped: editable with no system mounted at all."""

    @pytest.mark.asyncio
    async def test_an_arm_refuses_it(self, empty_client: AsyncClient) -> None:
        resp = await empty_client.patch("/move-defaults/pf400", json=CONTRADICTION)

        assert resp.status_code == 400
        assert "z_offset" in resp.text

    @pytest.mark.asyncio
    async def test_a_grip_profile_refuses_it(self, empty_client: AsyncClient) -> None:
        resp = await empty_client.patch("/grip-profiles/costar_96", json=CONTRADICTION)

        assert resp.status_code == 400
        assert "z_offset" in resp.text

    @pytest.mark.asyncio
    async def test_a_field_the_position_owns_is_refused_for_that_first(
        self, empty_client: AsyncClient,
    ) -> None:
        """Naming one in both halves is beside the point when it cannot be
        stored at this layer at all: answering the collision would send the
        caller back to be refused a second time for the real reason."""
        resp = await empty_client.patch(
            "/move-defaults/pf400",
            json={"set": {"clearance": 3.0}, "clear": ["clearance"]},
        )

        assert resp.status_code == 400
        assert "position" in resp.text, resp.text

    @pytest.mark.asyncio
    async def test_a_grip_profile_refuses_a_position_owned_field_for_that_first(
        self, empty_client: AsyncClient,
    ) -> None:
        """Both wider layers order the two rules the same way. Pinning it on
        one leaves the other free to answer the less useful reason."""
        resp = await empty_client.patch(
            "/grip-profiles/costar_96",
            json={"set": {"clearance": 3.0}, "clear": ["clearance"]},
        )

        assert resp.status_code == 400
        assert "position" in resp.text, resp.text

    @pytest.mark.asyncio
    async def test_the_edit_still_lands_when_the_halves_name_different_fields(
        self, empty_client: AsyncClient,
    ) -> None:
        """The refusal is about one field named twice, not about using both
        halves: that pairing is the reason they travel together."""
        resp = await empty_client.patch(
            "/move-defaults/pf400",
            json={"set": {"z_offset": 3.0}, "clear": ["speed"]},
        )

        assert resp.status_code == 200


_COORDS = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0, "pitch": 90.0, "roll": 180.0}


class TestTheNarrowestLayerThatTakesAPair:
    @pytest.mark.asyncio
    async def test_one_labware_at_one_position_refuses_it(
        self, client: AsyncClient,
    ) -> None:
        await client.post("/access-configs", json={
            "name": "vert_a", "access_type": "vertical", "gripper_offset": 20.0,
            "vertical_clearance": 20.0, "horizontal_clearance": 100.0,
        })
        await client.post("/teachpoints", json={
            "device_id": "robot1", "position_id": "hotel_3",
            "coord_type": "cartesian", "coords": dict(_COORDS),
            "access_config_name": "vert_a", "orientation": "right",
        })

        resp = await client.patch(
            "/teachpoints/robot1/hotel_3/labware/costar_96", json=CONTRADICTION,
        )

        assert resp.status_code == 400
        assert "z_offset" in resp.text


class TestAFieldOnlyTheLabwareCanAnswerFor:
    """An arm-wide grip depth is never read, so storing one is worse than
    refusing it: the operator sets a number, reads it back, and the arm keeps
    gripping wherever the labware's own profile says.
    """

    def test_setting_it_arm_wide_is_refused(self) -> None:
        from orca.runtime.move_parameters import reject_labware_owned

        with pytest.raises(MoveParameterEditRefused) as refused:
            reject_labware_owned(
                MoveParameterPatch(grip_distance_from_top=6.0),
            )

        assert refused.value.fields == ["grip_distance_from_top"]
        assert "grip-profiles set" in str(refused.value)

    def test_clearing_it_arm_wide_is_refused_too(self) -> None:
        from orca.runtime.move_parameters import reject_labware_owned

        with pytest.raises(MoveParameterEditRefused):
            reject_labware_owned(
                MoveParameterPatch(), clear=["grip_distance_from_top"],
            )

    def test_the_grip_profile_layer_still_takes_it(self) -> None:
        """The refusal is about the arm-wide layer only. `reject_site_owned` is
        what a grip profile is checked against, and it says nothing here."""
        from orca.runtime.move_parameters import reject_site_owned

        reject_site_owned(MoveParameterPatch(grip_distance_from_top=6.0))
