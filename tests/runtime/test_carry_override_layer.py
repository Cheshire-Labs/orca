"""This one piece of labware: the layer about an object rather than a kind.

Every layer above resolves from a type, a place, or an arm, so none of them can
say that THIS plate came out of the sealer with a lid on it. Without this layer
the only way to move one plate differently is to change how every plate of its
type moves.
"""

import pytest
from cheshire_drivers.move_parameters import MoveParameterPatch
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint

from orca.resource_models.labware import LabwareInstance
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.move_parameters import resolve_move_parameters
from orca.runtime.system_runtime import SystemRuntime
from tests.test_system_runtime import _build_simple_system


def _teachpoint(clearance: float = 20.0) -> Teachpoint:
    return Teachpoint(
        position_id="hotel_3",
        coordinates=CartesianCoordinates(
            x=1.0, y=2.0, z=3.0, yaw=0.0, pitch=90.0, roll=-180.0,
        ),
        orientation="left",
        access=AccessConfig(
            name="taught_vertical",
            access_type="vertical",
            gripper_offset=20.0,
            vertical_clearance=clearance,
            horizontal_clearance=100.0,
        ),
    )


class TestItWinsOverEveryOtherLayer:
    def test_it_beats_the_arm_defaults(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(speed=80.0),
            _teachpoint(),
            carry_patch=MoveParameterPatch(speed=30.0),
        )

        assert resolved.parameters.speed == 30.0
        assert resolved.sources["speed"] == "carry"

    def test_it_beats_the_labware_type_profile(self) -> None:
        """The type says how a costar_96 is held; this says how THIS one is."""
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(),
            MoveParameterPatch(resource_height=10.0),
            "costar_96",
            MoveParameterPatch(resource_height=30.0),
        )

        assert resolved.parameters.resource_height == 30.0

    def test_it_beats_the_position(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            _teachpoint(clearance=20.0),
            carry_patch=MoveParameterPatch(clearance=45.0),
        )

        assert resolved.parameters.clearance == 45.0

    def test_it_beats_this_labware_at_this_position(self) -> None:
        """Even the exception measured for this type here loses to a statement
        about the one object being moved right now."""
        teachpoint = _teachpoint()
        teachpoint.by_labware["deep_well"] = MoveParameterPatch(clearance=45.0)

        resolved = resolve_move_parameters(
            MoveParameterPatch(),
            teachpoint,
            None,
            "deep_well",
            MoveParameterPatch(clearance=60.0),
        )

        assert resolved.parameters.clearance == 60.0
        assert resolved.sources["clearance"] == "carry"


class TestItStaysSparse:
    def test_a_field_it_does_not_name_still_comes_from_underneath(self) -> None:
        resolved = resolve_move_parameters(
            MoveParameterPatch(travel_margin=25.0),
            _teachpoint(),
            carry_patch=MoveParameterPatch(speed=30.0),
        )

        assert resolved.parameters.travel_margin == 25.0
        assert resolved.sources["travel_margin"] == "defaults"

    def test_no_override_leaves_the_answer_exactly_as_it_was(self) -> None:
        without = resolve_move_parameters(MoveParameterPatch(), _teachpoint())
        with_empty = resolve_move_parameters(
            MoveParameterPatch(), _teachpoint(),
            carry_patch=MoveParameterPatch(),
        )

        assert with_empty == without


class TestSayingItOnTheLabware:
    def test_a_labware_starts_carrying_normally(self) -> None:
        assert LabwareInstance("t", "costar_96").carry_override == MoveParameterPatch()

    def test_naming_a_field_does_not_un_name_the_last_one(self) -> None:
        """Two decisions made minutes apart both hold; the second is not a
        replacement for the first."""
        plate = LabwareInstance("t", "costar_96")

        plate.carry_with(z_offset=2.0)
        plate.carry_with(speed=30.0)

        assert plate.carry_override == MoveParameterPatch(z_offset=2.0, speed=30.0)

    def test_none_hands_one_field_back_and_leaves_the_rest(self) -> None:
        plate = LabwareInstance("t", "costar_96")
        plate.carry_with(z_offset=2.0, speed=30.0)

        plate.carry_with(speed=None)

        assert plate.carry_override == MoveParameterPatch(z_offset=2.0)

    def test_carrying_normally_drops_everything(self) -> None:
        plate = LabwareInstance("t", "costar_96")
        plate.carry_with(z_offset=2.0, speed=30.0)

        plate.carry_normally()

        assert plate.carry_override == MoveParameterPatch()

    def test_a_field_that_is_not_a_move_parameter_is_refused(self) -> None:
        """A typo that silently did nothing would read as an edit that worked."""
        with pytest.raises(ValueError):
            LabwareInstance("t", "costar_96").carry_with(grip_height=2.0)

    def test_handing_back_a_field_that_does_not_exist_is_refused(self) -> None:
        """The same typo on the clearing half, which is the easier one to miss:
        nothing changes either way, so only an error distinguishes them."""
        with pytest.raises(ValueError):
            LabwareInstance("t", "costar_96").carry_with(grip_height=None)


class _StoreThatRemembersBeingTold(InMemoryLabwareStore):
    """Counts the writes, because reading back cannot see them.

    The in-memory store holds the very object the facade mutates, so a
    read-back answers correctly whether or not the write was ever handed over,
    and a durable store would have lost it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    async def register(
        self, instance: LabwareInstance, execution_id: str | None = None,
    ) -> None:
        self.writes.append(instance.id)
        await super().register(instance, execution_id)


class TestSayingItThroughTheVerbAnOperatorReaches:
    """The facade, not the model under it: what an operator writes has to reach
    the store, or it is gone at the next restart."""

    async def _runtime_holding_a_plate(
        self,
    ) -> tuple[SystemRuntime, _StoreThatRemembersBeingTold, LabwareInstance]:
        system, _ = await _build_simple_system()
        store = _StoreThatRemembersBeingTold()
        plate = LabwareInstance("plate_96", "96_well")
        system.add_labware(plate)
        await store.register(plate)
        runtime = SystemRuntime(system, labware_store=store)
        try:
            await runtime.start()
        except Exception:
            await runtime.shutdown()
            raise
        store.writes.clear()
        return runtime, store, plate

    async def test_what_it_says_is_handed_to_the_store_to_keep(self) -> None:
        runtime, store, plate = await self._runtime_holding_a_plate()
        try:
            await runtime.labware.set_carry_override(
                plate.id, MoveParameterPatch(z_offset=2.0), [], confirm=True,
            )

            assert store.writes == [plate.id], "the override never reached the store"
            stored = await store.get_by_id(plate.id)
            assert stored is not None
            assert stored.carry_override == MoveParameterPatch(z_offset=2.0)
        finally:
            await runtime.shutdown()

    async def test_setting_and_handing_back_land_in_one_write(self) -> None:
        runtime, store, plate = await self._runtime_holding_a_plate()
        try:
            await runtime.labware.set_carry_override(
                plate.id, MoveParameterPatch(z_offset=2.0, speed=30.0), [], confirm=True,
            )
            await runtime.labware.set_carry_override(
                plate.id, MoveParameterPatch(clearance=5.0), ["speed"], confirm=True,
            )

            assert store.writes == [plate.id, plate.id], "one write per edit"
            stored = await store.get_by_id(plate.id)
            assert stored is not None
            assert stored.carry_override == MoveParameterPatch(z_offset=2.0, clearance=5.0)
        finally:
            await runtime.shutdown()

    async def test_handing_it_all_back_reaches_the_store_too(self) -> None:
        runtime, store, plate = await self._runtime_holding_a_plate()
        try:
            await runtime.labware.set_carry_override(
                plate.id, MoveParameterPatch(z_offset=2.0), [], confirm=True,
            )
            store.writes.clear()
            await runtime.labware.clear_carry_override(plate.id, confirm=True)

            assert store.writes == [plate.id], "handing it back never reached the store"
            stored = await store.get_by_id(plate.id)
            assert stored is not None
            assert stored.carry_override == MoveParameterPatch()
        finally:
            await runtime.shutdown()

    async def test_a_field_cannot_be_set_and_handed_back_in_one_call(self) -> None:
        """The facade is where this rule lives for all four layers, so it has to
        hold on a call that never passes through an operation."""
        runtime, store, plate = await self._runtime_holding_a_plate()
        try:
            with pytest.raises(ValueError, match="z_offset"):
                await runtime.labware.set_carry_override(
                    plate.id, MoveParameterPatch(z_offset=2.0), ["z_offset"],
                    confirm=True,
                )

            assert store.writes == [], "a refused edit must not reach the store"
        finally:
            await runtime.shutdown()

    async def test_an_unknown_plate_is_refused_rather_than_quietly_doing_nothing(
        self,
    ) -> None:
        runtime, _, _ = await self._runtime_holding_a_plate()
        try:
            with pytest.raises(KeyError):
                await runtime.labware.set_carry_override(
                    "does-not-exist", MoveParameterPatch(z_offset=2.0), [], confirm=True,
                )
            with pytest.raises(KeyError):
                await runtime.labware.clear_carry_override(
                    "does-not-exist", confirm=True,
                )
        finally:
            await runtime.shutdown()
